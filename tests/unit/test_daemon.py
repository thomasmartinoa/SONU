from __future__ import annotations

import asyncio
import builtins
import io
import json
import socket
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.config.paths import SonuPaths
from sonu.config.settings import AppsSettings, McpSettings, Settings, VerifySettings, VoiceSettings
from sonu.daemon.client import Notice, parse, try_run
from sonu.daemon.protocol import decode, encode
from sonu.daemon.server import KEEP_LOADED, Daemon
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_ollama import FakeOllama

LAUNCH_KITTY = {
    "kind": "steps",
    "message": "",
    "steps": [{"tool": "apps.launch", "args": {"app": "kitty", "workspace": 2}}],
}
WAKE_ON = {"kind": "steps", "message": "", "steps": [{"tool": "voice.wake", "args": {"on": True}}]}
WAKE_OFF = {"kind": "steps", "message": "", "steps": [{"tool": "voice.wake", "args": {"on": False}}]}
CLOSE_ZEN = {
    "kind": "steps",
    "message": "",
    "steps": [{"tool": "windows.close", "args": {"window": {"app": "zen"}}}],
}


# ---------------------------------------------------------------- the client's argument parsing
def test_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO())  # not a terminal: questions go to the picker
    assert parse(["open", "kitty", "on", "2"]) == {
        "type": "request",
        "dry_run": False,
        "json": False,
        "notify": False,
        "confirm": "gui",
        "text": "open kitty on 2",
    }
    listen = parse(["listen"])  # the SONU key
    assert listen is not None and listen["type"] == "listen" and listen["text"] == ""
    assert parse(["voice", "setup"]) is None
    options = parse(["--dry-run", "--confirm", "never", "-v", "close", "zen"])
    assert options is not None and options["dry_run"] and options["confirm"] == "never"
    assert options["text"] == "close zen"
    for argv in (
        ["doctor"],
        ["daemon", "status"],
        ["--deep", "open", "x"],
        ["--model", "m", "x"],
        [],
        ["--help"],
    ):
        assert parse(argv) is None  # the full CLI handles these


def test_protocol_rejects_what_isnt_a_message() -> None:
    assert decode(encode({"type": "plan", "steps": 1})) == {"type": "plan", "steps": 1}
    for line in (b"[1, 2]\n", b'{"no": "type"}\n', b"not json\n"):
        with pytest.raises(ValueError):
            decode(line)


def test_no_daemon_means_the_cli_runs_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert try_run(["open", "kitty"]) is None
    stale = tmp_path / "sonu" / "sonud.sock"  # left behind by a daemon that crashed
    stale.parent.mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(stale))
    listener.close()
    assert try_run(["open", "kitty"]) is None


# ---------------------------------------------------------------- the daemon, end to end
@dataclass
class Rig:
    fake: FakeHyprland
    ollama: FakeOllama
    run: Callable[..., Any]
    daemon: Daemon
    notices: list[tuple[str, str]]


class NoticeLog(Notice):
    """Records what would be shown on the desktop."""

    def __init__(self, shown: list[tuple[str, str]]) -> None:
        super().__init__(program="")
        self.shown = shown

    def show(self, title: str, body: str = "", *, seconds: float = 8.0, limit: int = 300) -> None:
        self.shown.append((title, body))


@pytest.fixture
async def rig(
    fake_hypr: FakeHyprland, short_tmp: Path, xdg_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Rig]:
    fake_hypr.apps.update({"kitty": FakeApp("kitty", "zsh"), "zen-browser": FakeApp("zen")})
    fake_hypr.windows.append(make_window("0xb2", "zen", "Zen Browser", workspace_id=1, stable_id="b2"))
    apps = Path(xdg_env["XDG_DATA_HOME"]) / "applications"
    write_desktop_files(apps)
    ollama = FakeOllama()
    ollama.add_model("qwen3.5:4b", size_gib=3.2, capabilities=["completion", "tools"])
    plans = {
        "open kitty on 2": LAUNCH_KITTY,
        "close zen": CLOSE_ZEN,
        "start listening for hey sonu": WAKE_ON,
        "stop listening for hey sonu": WAKE_OFF,
        "Listen for Hey Sonu": WAKE_ON,
        "which kitty?": {"kind": "ask", "message": "Did you mean kitty or Kitty URL Launcher?", "steps": []},
    }

    def respond(body: dict[str, Any]) -> str:
        if "format" not in body:  # the natural reply (free text), not a plan
            return "Hey! Not much — what can I do for you?" if "hello" in str(body) else "Done, kitty's on 2."
        return json.dumps(
            plans.get(body["messages"][-1]["content"], {"kind": "reply", "message": "Hi!", "steps": []})
        )

    ollama.chat_responder = respond
    env = {**xdg_env, **fake_hypr.env, "XDG_DATA_DIRS": str(short_tmp / "none")}
    runtime = short_tmp / "run"  # the client finds the daemon under $XDG_RUNTIME_DIR/sonu/
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", fake_hypr.signature)
    settings = Settings(apps=AppsSettings(launch_timeout_s=2.0), verify=VerifySettings(timeout_s=1.0))
    daemon = Daemon(settings, SonuPaths.from_env(env), env, ollama_transport=ollama.transport)
    await daemon.start(runtime / "sonu" / "sonud.sock")
    serving = asyncio.create_task(daemon.serve_forever())

    async def run(*argv: str, answers: list[str] | None = None) -> tuple[int | None, str]:
        replies = iter(answers or [])
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(replies))
        out = io.StringIO()
        code = await asyncio.to_thread(try_run, list(argv), out, NoticeLog(notices))
        return code, out.getvalue()

    notices: list[tuple[str, str]] = []
    yield Rig(fake_hypr, ollama, run, daemon, notices)
    serving.cancel()
    await daemon.stop()


@pytest.mark.anyio
async def test_a_request_through_the_daemon(rig: Rig) -> None:
    code, out = await rig.run("open", "kitty", "on", "2")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("plan by qwen3.5:4b in ") and lines[0].endswith("· 1 step")
    assert lines[1:] == [
        "✔ Opened kitty on workspace 2",
        "» Done, kitty's on 2.",
        "Done — every step verified",
    ]
    assert [w["workspace"]["id"] for w in rig.fake.windows_of_class("kitty")] == [2]
    # The model was loaded at start and kept (no cold start), and a practice plan warmed it up.
    assert rig.ollama.keep_alive["qwen3.5:4b"] == KEEP_LOADED
    assert rig.ollama.chat_requests[0]["messages"][-1]["content"] == "what's open right now?"


@pytest.mark.anyio
async def test_a_confirmation_is_asked_in_the_clients_terminal(rig: Rig) -> None:
    code, out = await rig.run("--confirm", "terminal", "close", "zen", answers=["n"])
    assert code == 1
    assert "? Close zen" in out and "Nothing changed — not approved" in out
    assert rig.fake.windows_of_class("zen")
    code, out = await rig.run("--confirm", "terminal", "close", "zen", answers=["y"])
    assert code == 0 and "✔ Closed zen" in out
    assert not rig.fake.windows_of_class("zen")


@pytest.mark.anyio
async def test_nobody_to_ask_means_risky_steps_are_refused(rig: Rig) -> None:
    code, out = await rig.run("--confirm", "never", "close", "zen")
    assert code == 1 and "⊘ Skipped (not approved)" in out
    assert rig.fake.windows_of_class("zen")


@pytest.mark.anyio
async def test_replies_and_json(rig: Rig) -> None:
    code, out = await rig.run("hello")
    assert (code, out) == (0, "» Hey! Not much — what can I do for you?\n")  # a natural reply, not the plan's
    code, out = await rig.run("--json", "open", "kitty", "on", "2")
    assert code == 0 and json.loads(out)["status"] == "done"


def test_the_service_starts_and_stops_with_the_hyprland_session() -> None:
    from sonu.cli.daemon_cmd import unit_text

    unit = unit_text("/home/me/.local/bin/sonu")
    assert "ExecStart=/home/me/.local/bin/sonu daemon run" in unit
    assert "PartOf=graphical-session.target" in unit and "WantedBy=graphical-session.target" in unit


# ---------------------------------------------------------------- voice (`sonu listen`, the SONU key)
@dataclass
class FakeEars:
    heard: list[str]

    def load(self) -> None: ...

    def transcribe(self, pcm16: bytes, hint: str = "") -> str:
        return self.heard.pop(0) if self.heard else ""


@dataclass
class FakeVoice:
    said: list[str] = field(default_factory=list)
    rate: int = 24000

    def load(self) -> None: ...

    def speak(self, text: str) -> bytes:
        self.said.append(text)
        return b"\x00" * 8


def voice_rig(rig: Rig, *heard: str) -> FakeVoice:
    from sonu.voice.audio import Utterance
    from sonu.voice.session import VoiceSession

    async def recorder(endpointing: Any, detector: Any) -> Utterance:
        utterance = Utterance()
        utterance.spoke = bool(ears.heard)
        return utterance

    ears, voice = FakeEars(list(heard)), FakeVoice()
    rig.daemon._voice = VoiceSession(ears, voice, recorder=recorder, speaker_argv=("true",))  # type: ignore[arg-type]
    return voice


@pytest.mark.anyio
async def test_listen_hears_acts_and_says_the_reply(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2")
    code, out = await rig.run("listen")
    assert code == 0
    assert out.splitlines()[:2] == ["🎙 Listening…", "You: open kitty on 2"]
    assert "✔ Opened kitty on workspace 2" in out
    assert voice.said == ["How can I help?", "Done, kitty's on 2."]  # a greeting, then the reply
    # Found on the laptop: from the SONU key there's no terminal, so the desktop shows each stage.
    assert rig.notices == [
        ("🎙 Listening…", "Say what you need"),
        ("You: open kitty on 2", "Working on it…"),
        ("SONU", "✔ Opened kitty on workspace 2\n» Done, kitty's on 2.\nDone — every step verified"),
        ("🎙 Still listening…", "» Done, kitty's on 2.\nDone — every step verified"),  # a follow-up?
        ("SONU", "✔ Opened kitty on workspace 2\n» Done, kitty's on 2.\nDone — every step verified"),  # no
    ]


@pytest.mark.anyio
async def test_greetings_take_turns_are_kept_and_use_the_name(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2")
    session = rig.daemon._voice
    assert session is not None
    await session.warm_up("Martin")
    assert "Yes, Martin?" in voice.said
    voice.said.clear()
    await session.greet("Martin")
    await session.greet("Martin")
    assert voice.said == []  # prepared at warm-up: nothing to render when the key is pressed
    assert [session.greeting(None) for _ in range(5)] == [
        "I'm listening.",
        "Yes?",
        "How can I help?",
        "What can I do for you?",
        "I'm listening.",
    ]


@pytest.mark.anyio
async def test_no_greeting_when_turned_off(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2")
    rig.daemon.settings = rig.daemon.settings.model_copy(update={"voice": VoiceSettings(greet=False)})
    await rig.run("listen")
    assert voice.said == ["Done, kitty's on 2."]


@pytest.mark.anyio
async def test_nothing_heard_ends_quietly(rig: Rig) -> None:
    voice = voice_rig(rig)
    _, out = await rig.run("listen")
    assert out.splitlines() == ["🎙 Listening…", "You: (nothing)", "✖ I didn't hear anything"]
    assert voice.said == ["How can I help?"]


@pytest.mark.anyio
async def test_a_medium_risk_step_takes_a_spoken_yes(rig: Rig) -> None:
    voice = voice_rig(rig, "close zen", "yes, go ahead")
    _, out = await rig.run("--confirm", "never", "listen")
    assert "✔ Closed zen" in out
    assert voice.said[1] == "Should I close zen (workspace 1)? Say yes or no."
    assert not rig.fake.windows_of_class("zen")


@pytest.mark.anyio
async def test_an_unclear_answer_is_a_no(rig: Rig) -> None:
    voice_rig(rig, "close zen", "hmm what")
    _, out = await rig.run("--confirm", "never", "listen")
    assert "Nothing changed — not approved" in out
    assert rig.fake.windows_of_class("zen")


def test_notice_updates_one_notification(tmp_path: Path) -> None:
    program = tmp_path / "notify-send"
    log = tmp_path / "calls"
    program.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho 42\n')
    program.chmod(0o755)
    notice = Notice(str(program))
    notice.show("🎙 Listening…", "Say what you need")
    notice.show("You: --help", "")
    first, second = log.read_text().splitlines()
    assert "--replace-id" not in first
    assert "--replace-id=42" in second and "-- You: --help" in second


@pytest.mark.anyio
async def test_hey_sonu_turned_on_hears_acts_and_is_remembered(rig: Rig, tmp_path: Path) -> None:
    from tests.unit.test_wake import loud_parts, silence, speech

    voice = voice_rig(rig, "Hey Sonu, open kitty on 2")
    session = rig.daemon._voice
    assert session is not None
    session.detector = loud_parts
    rig.daemon.notice = lambda: NoticeLog(rig.notices)
    mic = tmp_path / "mic.raw"  # after the request that turns it on has finished
    mic.write_bytes(silence(0.3) + speech(0.9) + silence(1.5))
    # One frame shows the mic works; the speech comes once that request has finished (while SONU
    # is busy, what the mic hears is dropped).
    go = tmp_path / "go"
    rig.daemon.wake.argv = (
        "sh",
        "-c",
        f"head -c 960 /dev/zero; while [ ! -e {go} ]; do sleep 0.05; done; cat {mic}; exec sleep 30",
    )

    code, out = await rig.run(
        "--confirm", "terminal", "start", "listening", "for", "hey", "sonu", answers=["y"]
    )
    assert code == 0, out
    assert "? Listen for 'Hey Sonu' — the microphone stays on" in out
    assert "✔ Listening for 'Hey Sonu'" in out
    assert rig.daemon.wake.file.exists()  # still on after a restart
    assert json.loads((rig.daemon.path.parent / "state.json").read_text()) == {"wake_word": True}
    go.touch()  # now someone says it

    for _ in range(100):
        if rig.fake.windows_of_class("kitty"):
            break
        await asyncio.sleep(0.05)
    assert [w["workspace"]["id"] for w in rig.fake.windows_of_class("kitty")] == [2]
    for _ in range(40):
        if voice.said:
            break
        await asyncio.sleep(0.05)
    assert voice.said == ["Done, kitty's on 2."]  # no greeting: the request came with the name
    assert ("You: open kitty on 2", "Working on it…") in rig.notices  # on the desktop: no terminal

    code, out = await rig.run("stop", "listening", "for", "hey", "sonu")
    assert "✔ Stopped listening for 'Hey Sonu'" in out
    assert not rig.daemon.wake.listening and not rig.daemon.wake.file.exists()
    assert json.loads((rig.daemon.path.parent / "state.json").read_text()) == {"wake_word": False}


@pytest.mark.anyio
async def test_hey_sonu_cant_start_without_voice(rig: Rig) -> None:
    code, out = await rig.run(
        "--confirm", "terminal", "start", "listening", "for", "hey", "sonu", answers=["y"]
    )
    assert code == 1
    assert "voice isn't ready" in out or "voice extra" in out
    assert not rig.daemon.wake.file.exists()


@pytest.mark.anyio
async def test_a_misheard_name_reaches_the_planner_right(rig: Rig) -> None:
    # Found on real hardware: "listen for Hey Sonu" was heard as "Listen for Hey Soin" / "…Sovenue",
    # and the model asked "did you mean Hey Sonu?" instead of turning it on.
    voice = voice_rig(rig, "Listen for Hey Soin", "no")
    _, out = await rig.run("listen")
    assert "You: Listen for Hey Sonu" in out
    assert voice.said[1].startswith("Should I listen for 'Hey Sonu'")


@pytest.mark.anyio
async def test_a_question_is_shown_once(rig: Rig) -> None:
    # Found on real hardware: the notification said "Did you mean …? » Did you mean …?".
    _, out = await rig.run("which", "kitty?")
    assert out.count("Did you mean kitty or Kitty URL Launcher?") == 1
    assert rig.notices == [("SONU", "» Did you mean kitty or Kitty URL Launcher?")]


def test_short_answers_are_in_the_speech_hint() -> None:
    # Found on real hardware: a spoken "yes" came out as "Peace".
    from sonu.daemon.server import speech_hint
    from sonu.desktop.apps.catalog import AppCatalog

    assert speech_hint(AppCatalog(entries=())).startswith("Hey Sonu. Yes. No.")


# ---------------------------------------------------------------- a conversation (follow-ups)
@pytest.mark.anyio
async def test_after_a_reply_sonu_keeps_listening_for_the_next_request(rig: Rig) -> None:
    # The owner: "after one command I need to call Hey Sonu again — keep hearing me a little".
    voice = voice_rig(rig, "open kitty on 2", "hello")
    code, out = await rig.run("listen")
    assert code == 0
    lines = out.splitlines()
    assert lines.count("🎙 Still listening… (“thanks” to finish)") == 2  # after each reply; then silence
    assert "You: hello" in lines
    assert voice.said == ["How can I help?", "Done, kitty's on 2.", "Hey! Not much — what can I do for you?"]


@pytest.mark.anyio
async def test_thanks_ends_the_conversation(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2", "thank you")
    planned = len(rig.ollama.chat_requests)
    _, out = await rig.run("listen")
    for _ in range(40):  # the client is done at the result; the daemon may still be saying the reply
        if voice.said[-1] == "You're welcome.":
            break
        await asyncio.sleep(0.05)
    assert voice.said[-1] == "You're welcome."
    assert out.splitlines()[-1] == "You: thank you"
    assert (
        len(rig.ollama.chat_requests) - planned == 2
    )  # the kitty plan and its reply; "thanks" isn't planned


@pytest.mark.anyio
async def test_hey_sonu_again_in_a_follow_up_is_fine(rig: Rig) -> None:
    voice_rig(rig, "open kitty on 2", "Hey Soin, hello")
    _, out = await rig.run("listen")
    assert "You: hello" in out.splitlines()


@pytest.mark.anyio
async def test_follow_ups_can_be_turned_off(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2", "hello")
    rig.daemon.settings = rig.daemon.settings.model_copy(update={"voice": VoiceSettings(follow_up_s=0)})
    _, out = await rig.run("listen")
    assert "Still listening" not in out
    for _ in range(40):  # the client is done at the result; the daemon still says the reply
        if len(voice.said) == 2:
            break
        await asyncio.sleep(0.05)
    assert voice.said == ["How can I help?", "Done, kitty's on 2."]


@pytest.mark.parametrize(
    ("text", "ends"),
    [
        ("thanks", True),
        ("Thank you.", True),
        ("that's all", True),
        ("Nothing else", True),
        ("never mind", True),
        ("no thanks", True),
        ("thanks Sonu", True),
        ("Thank you very much!", True),
        ("stop", True),
        ("stop the music", False),  # a request, not the end of the conversation
        ("thanks, and open kitty", False),
        ("open kitty", False),
        ("what's my battery", False),
        ("pause the music", False),
    ],
)
def test_ends_conversation(text: str, ends: bool) -> None:
    from sonu.daemon.server import ends_conversation

    assert ends_conversation(text) is ends


@pytest.mark.anyio
async def test_talking_over_the_reply_stops_it_and_sonu_listens(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2", "hello")
    session = rig.daemon._voice
    assert session is not None
    watched: list[str] = []

    async def talked_over(spoken: str) -> bool:
        watched.append(spoken)
        return spoken.startswith("Done")  # the user talks over the first reply only

    session.interruptions = talked_over
    _, out = await rig.run("listen")
    assert watched == ["Done, kitty's on 2.", "Hey! Not much — what can I do for you?"]  # replies only
    assert "You: hello" in out.splitlines()  # after being talked over, SONU listened
    assert voice.said[0] == "How can I help?"


@pytest.mark.anyio
async def test_by_voice_a_written_prompt_is_shown_then_asked_briefly(rig: Rig) -> None:
    from sonu.core.engine import ConfirmRequest
    from sonu.daemon.client import Progress
    from sonu.daemon.server import _DesktopSession
    from sonu.tools.base import Risk

    voice = voice_rig(rig, "yes")
    session = rig.daemon._voice
    assert session is not None

    async def physical(request: ConfirmRequest) -> bool:
        raise AssertionError("medium risk is answered by voice")

    shown: list[tuple[str, str]] = []
    confirm = rig.daemon._voice_confirmer(session, physical, _DesktopSession(Progress(NoticeLog(shown))))
    request = ConfirmRequest(
        "Send the prompt I wrote from your idea to Claude Code in todo-app: “Build a small todo app.…”",
        "dev.prompt",
        Risk.MEDIUM,
        "medium-risk action",
        details="Build a small todo app.\nRequirements:\n- a dark mode",
        spoken="send the prompt I wrote to Claude Code in todo-app — it's on your screen",
    )
    assert await confirm(request) is True
    assert shown == [("Read this first", "Build a small todo app.\nRequirements:\n- a dark mode")]
    assert voice.said == [
        "Should I send the prompt I wrote to Claude Code in todo-app — it's on your screen? Say yes or no."
    ]


# ---------------------------------------------------------------- the activity journal
async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(150):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not in time")


@pytest.mark.anyio
async def test_sonud_records_which_window_has_the_focus(rig: Rig) -> None:
    journal = rig.daemon.journal
    assert journal is not None and journal.path is not None
    assert journal.path.parent == rig.daemon.paths.data_dir
    await rig.fake.wait_for_event_clients(1)
    await rig.fake.emit("activewindow", "kitty,nvim dataset.py")
    await rig.fake.emit("activewindowv2", "c3")
    await rig.fake.emit("windowtitlev2", "c3,nvim notes.txt")
    await _until(lambda: journal.count() >= 1 and journal.spans(0, 2e9)[-1].title == "nvim notes.txt")
    assert journal.spans(0, 2e9)[-1].app == "kitty"
    # The CLI pauses it (a file sonud sees): nothing more is recorded.
    journal.pause(True)
    await rig.fake.emit("activewindow", "zen,News — Zen Browser")
    await asyncio.sleep(0.1)
    assert all(span.app != "zen" for span in journal.spans(0, 2e9))
    journal.pause(False)


@pytest.mark.anyio
async def test_the_journal_finds_hyprland_when_its_environment_is_stale(rig: Rig) -> None:
    # sonud can start before the session exports HYPRLAND_INSTANCE_SIGNATURE (or keep an old one).
    rig.daemon.env["HYPRLAND_INSTANCE_SIGNATURE"] = "gone_1_2"
    rig.daemon._client_env = {}
    assert rig.daemon._journal_instance().signature == rig.fake.signature


@pytest.mark.anyio
async def test_the_mcp_servers_the_user_added_start_with_sonud(
    short_tmp: Path, xdg_env: dict[str, str]
) -> None:
    ollama = FakeOllama()
    ollama.add_model("qwen3.5:4b", size_gib=3.2, capabilities=["completion", "tools"])
    ollama.chat_responder = lambda body: json.dumps({"kind": "reply", "message": "Hi!", "steps": []})
    command = [sys.executable, "-m", "sonu.testing.fake_mcp_server"]
    settings = Settings(
        mcp=McpSettings.model_validate({"servers": {"notes": {"command": command, "reaches": "local"}}})
    )
    daemon = Daemon(settings, SonuPaths.from_env(xdg_env), xdg_env, ollama_transport=ollama.transport)
    await daemon.warm_up()
    assert daemon.registry.get("mcp.notes.add_note") is not None
    # Before the practice plan: the instructions Ollama keeps cached already list them.
    assert "mcp.notes.add_note" in ollama.chat_requests[0]["messages"][0]["content"]
    assert daemon.outside is not None
    connection = daemon.outside.servers[0].connection
    await daemon.stop()
    assert connection is not None and not connection.alive
