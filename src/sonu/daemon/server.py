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
import logging
import os
import socket
import struct
from collections.abc import Mapping
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
from sonu.voice.engines import (
    KokoroVoice,
    VoiceUnavailable,
    WhisperEars,
    missing_files,
    silero_detector,
    voice_dir,
    voice_installed,
)
from sonu.voice.session import VoiceSession
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


class _Relay(Observer):
    """Sends progress to the client as it happens."""

    def __init__(self, session: _Session, model: str) -> None:
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


def speech_hint(catalog: AppCatalog) -> str:
    """What Whisper should expect to hear: SONU's name and the installed apps' names."""
    names = sorted({entry.name for entry in catalog.entries if not entry.no_display}, key=len)[:40]
    return "Sonu, workspace, " + ", ".join(names)


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
                    await self.voice_session().warm_up()
                except VoiceUnavailable as exc:
                    logger.warning("voice isn't ready: %s", exc)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("start() first")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._warming is not None:
            self._warming.cancel()
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

    async def _handle(self, session: _Session, request: dict[str, Any], pid: int) -> None:
        env = {**self.env, **{k: str(v) for k, v in request.get("env", {}).items() if k in ENV_KEYS}}
        try:
            engine = await self._engine(env, pid)
        except HyprlandError as exc:
            await session.send({"type": "error", "message": f"{exc} — is Hyprland running?"})
            return
        relay = _Relay(session, self.model)
        mode = request.get("confirm", "terminal")
        confirm, choose = self._askers(session, relay, mode)
        voice: VoiceSession | None = None
        if request["type"] == "listen":
            try:
                voice = self.voice_session()
            except VoiceUnavailable as exc:
                await session.send({"type": "error", "message": f"Voice isn't ready: {exc}"})
                return
            voice.hint = speech_hint(engine.ctx.resolver.catalog)
            await session.send({"type": "listening"})
            text = await voice.hear()
            await session.send({"type": "heard", "text": text})
            if not text:
                await session.send(final_message(TaskReport(request="", error="I didn't hear anything")))
                return
            request = {**request, "text": text}
            confirm = self._voice_confirmer(voice, confirm)

        async def run() -> TaskReport:
            return await engine.run(
                str(request.get("text", "")),
                confirm=confirm,
                choose=choose,
                dry_run=bool(request.get("dry_run")),
                observer=relay,
            )

        task = asyncio.create_task(run())
        watcher = asyncio.create_task(self._read_answers(session, task))
        try:
            while not task.done():
                await relay.flush()
                await asyncio.wait({task}, timeout=0.05)
            report = task.result()
        except asyncio.CancelledError:
            return  # the client left (Ctrl+C): stop, like the CLI does
        finally:
            watcher.cancel()
        await relay.flush()
        await session.send(final_message(report))
        if voice is not None and self.settings.voice.speak_replies:
            said = report.reply or (report.headline() if report.plan is not None else report.error or "")
            await voice.say(said)

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
            self._voice = VoiceSession(
                WhisperEars(settings.stt_model, settings.device),
                KokoroVoice(folder, settings.voice, settings.speed),
                detector=silero_detector(),
            )
        return self._voice

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

    def _askers(self, session: _Session, relay: _Relay, mode: str) -> tuple[Any, Any]:
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


async def run_daemon(settings: Settings, paths: SonuPaths, env: Mapping[str, str]) -> None:
    daemon = Daemon(settings, paths, env)
    path = await daemon.start()
    logger.info("sonud listening on %s (planner %s kept loaded)", path, daemon.model)
    try:
        await daemon.serve_forever()
    finally:
        await daemon.stop()
