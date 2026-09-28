"""`sonud`: SONU kept running, so a request costs planning time only.

* Built once: the Ollama client and planner (the model stays loaded while the daemon runs — no
  6-second cold start), the policy, the audit log, the tools. Per request it refreshes what can
  change: the Hyprland instance (from the client's environment, so a new login works), the app
  catalog and the rice's defaults.
* One request at a time. Questions (confirmations, "which one?") go back to the client that asked,
  or to the rofi/fuzzel picker when it has no terminal; if the client goes away (Ctrl+C), the
  request is cancelled — like the CLI's own Ctrl+C.
* Private socket: ``$XDG_RUNTIME_DIR/sonu/sonud.sock`` (directory 0700, socket 0600), and every
  connection's peer must be the same user (SO_PEERCRED). The client's process id comes from the
  kernel, so SONU finds the terminal a request was typed in, as the CLI does.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import socket
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sonu.audit.log import AuditLog
from sonu.config.paths import SonuPaths
from sonu.config.settings import Settings
from sonu.core.engine import ConfirmRequest, Engine, Observer
from sonu.core.planner import LLMPlanner
from sonu.core.planning import Plan, PlanningError
from sonu.core.report import ICONS, StepRecord, TaskReport
from sonu.daemon.client import Notice, Progress
from sonu.daemon.protocol import ENV_KEYS, decode, encode, socket_path
from sonu.desktop.apps.catalog import AppCatalog
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.instance import discover_instance
from sonu.integrations.hypr_theme import detect_hypr_theme
from sonu.llm.chat import OllamaChatModel
from sonu.llm.ollama import OllamaClient, OllamaError
from sonu.policy.engine import Policy
from sonu.runtime import app_aliases, assistant_parts, load_memory
from sonu.tools.base import Choices, Risk, StepStatus
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry
from sonu.ui.desktop_ui import Picker
from sonu.voice.audio import RECORD_ARGV, Endpointing
from sonu.voice.echo import SINK as ECHO_SINK
from sonu.voice.echo import EchoCanceller
from sonu.voice.engines import (
    KokoroVoice,
    VoiceUnavailable,
    WhisperEars,
    missing_files,
    silero_detector,
    voice_dir,
    voice_installed,
)
from sonu.voice.interrupt import user_talked
from sonu.voice.session import VoiceSession
from sonu.voice.wake import WakeListener, after_wake_word, fix_name
from sonu.world.origin import ancestor_pids
from sonu.world.snapshot import WorldSnapshot

logger = logging.getLogger(__name__)

SAY = "» "  # how SONU's own words are marked, after the result lines
KEEP_LOADED = "-1m"  # Ollama: a negative duration keeps the model loaded for as long as the daemon runs


@dataclass
class _Session:
    """One connected client and the questions it's answering."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    answers: dict[int, asyncio.Future[Any]]

    async def send(self, message: dict[str, Any]) -> None:
        self.writer.write(encode(message))
        await self.writer.drain()


class _DesktopSession:
    """A spoken request nobody typed ("Hey Sonu"): progress goes to a desktop notification, and
    questions to the picker."""

    def __init__(self, progress: Progress | None) -> None:
        self.progress = progress
        self.answers: dict[int, asyncio.Future[Any]] = {}

    async def send(self, message: dict[str, Any]) -> None:
        if self.progress is not None:
            await asyncio.to_thread(self.progress.update, message)


class _Relay(Observer):
    """Sends progress to the client as it happens."""

    def __init__(self, session: _Session | _DesktopSession, model: str) -> None:
        self.session, self.model = session, model
        self.pending: list[dict[str, Any]] = []

    def _queue(self, message: dict[str, Any]) -> None:
        self.pending.append(message)

    def on_plan(self, plan: Plan) -> None:
        self._queue(
            {
                "type": "plan",
                "kind": plan.kind,
                "model": plan.model,
                "duration_s": plan.duration_s,
                "steps": len(plan.steps),
            }
        )

    def on_step_start(self, index: int, description: str) -> None:
        self._queue({"type": "step_start", "description": description})

    def on_step(self, record: StepRecord) -> None:
        summary = record.outcome.summary
        if record.outcome.status is StepStatus.SKIPPED:
            summary = f"would: {record.description}"
        self._queue(
            {
                "type": "step",
                "line": f"{ICONS[record.outcome.status]} {summary}",
                "status": record.outcome.status.value,
            }
        )

    async def flush(self) -> None:
        while self.pending:
            await self.session.send(self.pending.pop(0))


def final_message(report: TaskReport) -> dict[str, Any]:
    """What the client prints at the end — the same lines the CLI prints (print_final)."""
    if report.error:
        return {
            "type": "final",
            "status": report.status,
            "before_headline": [f"✖ {report.error}"],
            "headline": "",
            "report": report.as_dict(),
        }
    streamed = report.plan is not None and report.plan.kind == "steps" and bool(report.steps)
    before = [*report.not_done, *(f"? {q}" for q in report.questions)] if streamed else report.lines()
    before = [line for line in before if line != report.reply]  # found on the laptop: said twice
    if report.reply:
        if report.plan is not None and report.plan.kind == "reply":
            before = []  # the natural reply replaces the planner's short one
        before = [*before, f"{SAY}{report.reply}"]
    headline = report.headline() if report.plan is not None and report.plan.kind == "steps" else ""
    return {
        "type": "final",
        "status": report.status,
        "before_headline": before,
        "headline": headline,
        "report": report.as_dict(),
    }


# The conversation ended with nothing more to show (silence, or "thanks" after a follow-up).
QUIET_END: dict[str, Any] = {
    "type": "final",
    "status": "reply",
    "before_headline": [],
    "headline": "",
    "report": {},
}
_ENDS = re.compile(
    r"^\W*(thanks?|thank you|that'?s (all|it)|nothing( else)?|no,? thanks?|never ?mind|stop|bye|goodbye|"
    r"cancel|i'?m (done|good)|all good)(,? (sonu|very much|a lot))?\W*$",  # the whole of what was said
    re.I,
)


def ends_conversation(text: str) -> bool:
    return bool(_ENDS.match(text))


def speech_hint(catalog: AppCatalog) -> str:
    """What Whisper should expect to hear: SONU's name, short answers and the installed apps' names.
    Found on the laptop: a spoken "yes" came out as "Peace"."""
    names = sorted({entry.name for entry in catalog.entries if not entry.no_display}, key=len)[:40]
    return "Hey Sonu. Yes. No. Workspace, " + ", ".join(names)


class Daemon:
    def __init__(
        self,
        settings: Settings,
        paths: SonuPaths,
        env: Mapping[str, str],
        *,
        ollama_transport: httpx.AsyncBaseTransport | None = None,  # tests plug in a fake Ollama here
    ) -> None:
        self.settings, self.paths, self.env = settings, paths, dict(env)
        self.lock = asyncio.Lock()
        self.ollama = OllamaClient(
            settings.ollama.host, timeout_s=settings.ollama.timeout_s, transport=ollama_transport
        )
        self.ollama_settings = settings.ollama.model_copy(update={"keep_alive": KEEP_LOADED})
        self.model = settings.models.planner.model
        self.planner = LLMPlanner(OllamaChatModel(self.ollama, settings.models.planner, self.ollama_settings))
        self.audit = AuditLog(paths.audit_file) if settings.audit.enabled else None
        self.registry = ToolRegistry()
        self.policy = Policy(settings.policy.autonomy)
        self._ids = itertools.count(1)
        self._server: asyncio.Server | None = None
        self._warming: asyncio.Task[None] | None = None
        self._voice: VoiceSession | None = None
        self.wake = _WakeWord(self)
        self.echo = EchoCanceller()
        self.notice: Callable[[], Notice] = Notice  # tests record instead of showing
        self._client_env: dict[str, str] = {}  # the latest client's (for requests nobody typed)
        self._waking = False  # a "Hey Sonu" request is starting
        self._wake_request_task: asyncio.Task[None] | None = None
        self._quiet_until = 0.0  # SONU just spoke: its own voice isn't a wake word

    # -------------------------------------------------------------- lifetime
    async def start(self, path: Path | None = None) -> Path:
        path = path or socket_path(self.env)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()  # a socket left by a daemon that didn't stop cleanly
        self._server = await asyncio.start_unix_server(self._serve, path=str(path))
        path.chmod(0o600)
        self.path = path
        self._warming = asyncio.create_task(self.warm_up())
        return path

    async def warm_up(self) -> None:
        """Load the planner model and plan once, holding requests until then.

        Found on the owner's laptop: the first request took 12 s — the model was still loading and
        then had to read SONU's long instructions. A practice plan leaves them cached in Ollama
        (the desktop part comes last in the prompt, so every real request reuses the rest).
        """
        async with self.lock:
            try:
                await self.ollama.load(self.model, keep_alive=KEEP_LOADED)
                await self.planner.plan("what's open right now?", WorldSnapshot(windows=()), self.registry)
            except (OllamaError, PlanningError) as exc:  # Ollama may start later: the first request pays
                logger.warning("couldn't warm up %s: %s", self.model, exc)
            if voice_installed() and not missing_files(voice_dir(self.paths.data_dir)):
                try:  # the ears and voice too, so the first press of the key answers at once
                    await self.voice_session().warm_up(self._user_name())
                except VoiceUnavailable as exc:
                    logger.warning("voice isn't ready: %s", exc)
                    return
                # the user turned "Hey Sonu" on earlier
                if self.wake.file.exists() and (problem := await self.wake.set(True)):
                    logger.warning("couldn't listen for 'Hey Sonu': %s", problem)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("start() first")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._warming is not None:
            self._warming.cancel()
        await self.wake.stop()
        self.echo.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError, AttributeError):
            self.path.unlink()
        with contextlib.suppress(OllamaError):
            await self.ollama.load(self.model, keep_alive="0")  # give the GPU memory back
        await self.ollama.aclose()

    # -------------------------------------------------------------- one client
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = _Session(reader, writer, {})
        try:
            pid = self._peer(writer)
            if pid is None:
                return
            request = decode(await reader.readline())
            if request.get("type") not in ("request", "listen"):
                return
            if request["type"] == "listen" and self._voice is not None:
                self._voice.stop_talking()  # pressing the key while SONU talks cuts it short
            async with self.lock:
                await self._handle(session, request, pid)
        except (ConnectionError, ValueError, asyncio.IncompleteReadError) as exc:
            logger.info("client went away: %s", exc)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    @staticmethod
    def _peer(writer: asyncio.StreamWriter) -> int | None:
        """The client's pid, if it runs as this user (SO_PEERCRED); None refuses the connection."""
        sock = writer.get_extra_info("socket")
        if sock is None:
            return None
        pid, uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        return pid if uid == os.getuid() else None

    async def _handle(self, session: _Session | _DesktopSession, request: dict[str, Any], pid: int) -> None:
        client_env = {k: str(v) for k, v in request.get("env", {}).items() if k in ENV_KEYS}
        if isinstance(session, _Session):
            self._client_env = client_env
        env = {**self.env, **client_env}
        try:
            engine = await self._engine(env, pid)
        except HyprlandError as exc:
            await session.send({"type": "error", "message": f"{exc} — is Hyprland running?"})
            return
        relay = _Relay(session, self.model)
        mode = request.get("confirm", "terminal")
        confirm, choose = self._askers(session, relay, mode)
        if request["type"] != "listen":
            report = await self._run(
                engine, session, relay, str(request.get("text", "")), confirm, choose, request
            )
            if report is not None:
                await session.send(final_message(report))
            return
        try:
            voice = self.voice_session()
        except VoiceUnavailable as exc:
            await session.send({"type": "error", "message": f"Voice isn't ready: {exc}"})
            return
        voice.hint = speech_hint(engine.ctx.resolver.catalog)
        confirm = self._voice_confirmer(voice, confirm)
        text = str(request.get("heard") or "")  # "Hey Sonu, open kitty": already heard
        if not text:
            if self.settings.voice.greet:
                await voice.greet(self._user_name())
            await session.send({"type": "listening"})
            text = fix_name(await voice.hear())
        await session.send({"type": "heard", "text": text})
        if not text:
            await session.send(final_message(TaskReport(request="", error="I didn't hear anything")))
            return
        while True:
            report = await self._run(engine, session, relay, text, confirm, choose, request)
            if report is None:
                return
            follow_up = self.settings.voice.follow_up_s > 0 and not request.get("dry_run")
            await session.send({**final_message(report), "more": follow_up})
            if self.settings.voice.speak_replies:
                said = report.reply or (report.headline() if report.plan is not None else report.error or "")
                await voice.say(said, interruptible=follow_up)  # talked over: it stops and listens
            if not follow_up:
                return
            follow = await self._follow_up(session, voice)
            if follow is None:
                return
            text = follow

    async def _follow_up(self, session: _Session | _DesktopSession, voice: VoiceSession) -> str | None:
        """After a spoken request, keep listening a little: the next request needs no key and no
        "Hey Sonu". Silence, or "thanks" / "that's all", ends the conversation (None).

        The owner: "I need it to keep hearing me after one command … like a conversation flow."
        """
        await session.send({"type": "listening", "follow_up": True})
        heard = fix_name(await voice.hear(Endpointing(start_timeout_s=self.settings.voice.follow_up_s)))
        text = after_wake_word(heard) or heard if heard else ""  # "Hey Sonu, …" again is fine too
        if not text:
            await session.send(QUIET_END)
            return None
        await session.send({"type": "heard", "text": text})
        if ends_conversation(text):
            await session.send(QUIET_END)
            await voice.say("You're welcome." if re.match(r"\W*thank", text, re.I) else "Okay.")
            return None
        return text

    async def _run(
        self,
        engine: Engine,
        session: _Session | _DesktopSession,
        relay: _Relay,
        text: str,
        confirm: Any,
        choose: Any,
        request: dict[str, Any],
    ) -> TaskReport | None:
        """One request through the engine, streaming progress; None if the client went away."""

        async def run() -> TaskReport:
            return await engine.run(
                text, confirm=confirm, choose=choose, dry_run=bool(request.get("dry_run")), observer=relay
            )

        task = asyncio.create_task(run())
        watcher = (
            asyncio.create_task(self._read_answers(session, task)) if isinstance(session, _Session) else None
        )
        try:
            while not task.done():
                await relay.flush()
                await asyncio.wait({task}, timeout=0.05)
            report = task.result()
        except asyncio.CancelledError:
            return None  # the client left (Ctrl+C): stop, like the CLI does
        finally:
            if watcher is not None:
                watcher.cancel()
        await relay.flush()
        return report

    # -------------------------------------------------------------- voice
    def voice_session(self) -> VoiceSession:
        """The ears and the voice, built once (the models load on first use, then stay)."""
        if self._voice is None:
            if not voice_installed():
                raise VoiceUnavailable("the voice extra isn't installed — see `sonu voice setup`")
            settings = self.settings.voice
            folder = voice_dir(self.paths.data_dir)
            if missing_files(folder):
                raise VoiceUnavailable("the voice's model files aren't downloaded — run `sonu voice setup`")
            ears, detector = WhisperEars(settings.stt_model, settings.device), silero_detector()
            self._voice = VoiceSession(
                ears, KokoroVoice(folder, settings.voice, settings.speed), detector=detector
            )
            if settings.barge_in and self.echo.ensure():  # D50: SONU's voice through its echo canceller
                self._voice.speaker_argv = ("pw-play", "--target", ECHO_SINK)
                self._voice.interruptions = lambda spoken: user_talked(spoken, detector, ears.transcribe)
        return self._voice

    # -------------------------------------------------------------- "Hey Sonu"
    def _busy(self) -> bool:
        """SONU is working, talking, or just finished: what the mic hears isn't a wake word."""
        return self.lock.locked() or self._waking or time.monotonic() < self._quiet_until

    async def _on_wake(self, words: str) -> None:
        """SONU's name was said: handle it like a press of the key (in its own task, so turning
        "Hey Sonu" off by voice doesn't cancel the request doing it)."""
        if self._busy():
            return
        self._waking = True
        self._wake_request_task = asyncio.create_task(self._wake_request(words))

    async def _wake_request(self, words: str) -> None:
        try:
            async with self.lock:
                self._waking = False
                session = _DesktopSession(Progress(self.notice()))
                request = {"type": "listen", "confirm": "gui", "heard": words, "env": self._client_env}
                await self._handle(session, request, os.getpid())
        except Exception:  # never let a failed request stop the listening
            logger.exception("a 'Hey Sonu' request failed")
        finally:
            self._waking = False
            self._quiet_until = time.monotonic() + 0.8

    def _user_name(self) -> str | None:
        return load_memory(self.paths).name() or self.settings.assistant.user_name

    def _voice_confirmer(self, voice: VoiceSession, physical: Any) -> Any:
        """Medium risk: a spoken yes. High risk: said aloud, answered only on the desktop (D42)."""

        async def confirm(request: ConfirmRequest) -> bool:
            if request.risk is Risk.HIGH:
                await voice.say(f"This one needs your OK on screen: {request.description}.")
                return bool(await physical(request))
            return await voice.confirm_by_voice(request)

        return confirm

    async def _read_answers(self, session: _Session, task: asyncio.Task[TaskReport]) -> None:
        """Answers from the client; if it disconnects, the request is cancelled."""
        while True:
            line = await session.reader.readline()
            if not line:
                task.cancel()
                return
            message = decode(line)
            future = session.answers.pop(int(message.get("id", -1)), None)
            if message.get("type") == "answer" and future is not None and not future.done():
                future.set_result(message.get("value"))

    def _askers(self, session: _Session | _DesktopSession, relay: _Relay, mode: str) -> tuple[Any, Any]:
        """Confirm/choose through the client's terminal, the desktop picker, or nobody (refuse)."""
        if mode == "terminal":

            async def ask(message: dict[str, Any]) -> Any:
                await relay.flush()  # the question comes after the lines before it
                ask_id = next(self._ids)
                future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
                session.answers[ask_id] = future
                await session.send({**message, "id": ask_id})
                return await future

            async def confirm(request: ConfirmRequest) -> bool:
                answer = await ask(
                    {
                        "type": "confirm",
                        "description": request.description,
                        "risk": request.risk.value,
                        "reason": request.reason,
                    }
                )
                return answer is True

            async def choose(choices: Choices) -> str | None:
                offered = [[option.label, option.value] for option in choices.options]
                answer = await ask({"type": "choose", "title": choices.prompt, "options": offered})
                return (
                    answer if any(answer == value for _, value in offered) else None
                )  # only an offered answer

            return confirm, choose
        from sonu.cli.ask_cmd import gui_chooser, gui_confirmer  # the same dialogs the CLI uses

        picker = Picker.detect() if mode == "gui" else None
        if picker is None:

            async def refuse(request: ConfirmRequest) -> bool:
                return False

            return refuse, None
        return gui_confirmer(picker), gui_chooser(picker)

    async def _engine(self, env: Mapping[str, str], pid: int) -> Engine:
        """An engine for this request: fresh Hyprland connection, app catalog and rice defaults."""
        desktop = await HyprlandDesktop.connect(
            discover_instance(env), timeout_s=self.settings.hyprland.request_timeout_s
        )
        catalog = await asyncio.to_thread(AppCatalog.load, env)
        rice = detect_hypr_theme(Path(env.get("HOME") or Path.home()))
        role_defaults = {kind: app.command for kind, app in rice.defaults.items()}
        memory = load_memory(self.paths)  # read per request: "remember …" takes effect at once
        resolver = AppResolver(
            catalog, role_defaults=role_defaults, aliases=app_aliases(self.settings, memory)
        )
        ctx = ToolContext(
            desktop,
            resolver,
            self.settings,
            default_terminal=role_defaults.get("terminal"),
            ancestors=lambda: ancestor_pids(pid),  # the terminal the request was typed in
            memory=memory,
            wake=self.wake,
        )
        conversation, responder = assistant_parts(
            self.settings, self.paths, self.ollama, self.ollama_settings, memory
        )
        return Engine(
            planner=self.planner,
            registry=self.registry,
            ctx=ctx,
            policy=self.policy,
            audit=self.audit,
            conversation=conversation,
            responder=responder,
        )


class _WakeWord:
    """Listening for "Hey Sonu" (D47): on only when the user turns it on; remembered across restarts
    (a file in the voice folder); its state is in ``$XDG_RUNTIME_DIR/sonu/state.json`` for a bar."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.file = voice_dir(daemon.paths.data_dir) / "wake-word"
        self.task: asyncio.Task[None] | None = None
        self.argv: Sequence[str] = RECORD_ARGV  # the microphone (tests read a file)

    @property
    def listening(self) -> bool:
        return self.task is not None and not self.task.done()

    async def set(self, on: bool) -> str | None:
        if not on:
            await self.stop()
            self.file.unlink(missing_ok=True)
            self._publish()
            return None
        if self.listening:
            return None
        try:
            voice = self.daemon.voice_session()
        except VoiceUnavailable as exc:
            return f"voice isn't ready: {exc}"
        if voice.detector is None:
            return "no speech detector"
        listener = WakeListener(
            voice.ears.transcribe,
            voice.detector,
            self.daemon._on_wake,
            busy=self.daemon._busy,
            hint=lambda: voice.hint,
            argv=self.argv,
        )
        self.task = asyncio.create_task(listener.run())
        self.task.add_done_callback(self._ended)
        for _ in range(60):  # the check: audio really arrives from the microphone
            if listener.heard_frames or self.task.done():
                break
            await asyncio.sleep(0.05)
        if not listener.heard_frames:
            await self.stop()
            return "the microphone didn't start (pw-record)"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("on\n")
        self._publish()
        return None

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None

    def _ended(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("stopped listening for 'Hey Sonu': %s", task.exception())
        self._publish()

    def _publish(self) -> None:
        path = getattr(self.daemon, "path", socket_path(self.daemon.env)).parent / "state.json"
        with contextlib.suppress(OSError):
            path.write_text(json.dumps({"wake_word": self.listening}) + "\n")


async def run_daemon(settings: Settings, paths: SonuPaths, env: Mapping[str, str]) -> None:
    daemon = Daemon(settings, paths, env)
    path = await daemon.start()
    logger.info("sonud listening on %s (planner %s kept loaded)", path, daemon.model)
    try:
        await daemon.serve_forever()
    finally:
        await daemon.stop()
