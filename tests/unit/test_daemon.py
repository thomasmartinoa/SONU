from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import json
import re
import socket
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.config.paths import SonuPaths
from sonu.config.settings import AppsSettings, McpSettings, Settings, VerifySettings, VoiceSettings
from sonu.daemon import server
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
        "open vs code": {
            "kind": "steps",
            "message": "Opening VS Code.",
            "steps": [{"tool": "apps.launch", "args": {"app": "vs code"}}],
        },
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
    notices: list[tuple[str, str]] = []
    daemon.notice = lambda: NoticeLog(notices)  # never a real notification on the machine running the tests
    await daemon.start(runtime / "sonu" / "sonud.sock")
    serving = asyncio.create_task(daemon.serve_forever())

    async def run(*argv: str, answers: list[str] | None = None) -> tuple[int | None, str]:
        replies = iter(answers or [])
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(replies))
        out = io.StringIO()
        code = await asyncio.to_thread(try_run, list(argv), out, NoticeLog(notices))
        return code, out.getvalue()

    yield Rig(fake_hypr, ollama, run, daemon, notices)
    serving.cancel()
    await daemon.stop()


@pytest.mark.anyio
async def test_a_request_through_the_daemon(rig: Rig) -> None:
    code, out = await rig.run("open", "kitty", "on", "2")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("plan by qwen3.5:4b in ") and lines[0].endswith("· 1 step")
    assert lines[1:3] == ["✔ Opened kitty on workspace 2", "» Done, kitty's on 2."]
    # the whole time, not only the plan's (found on real hardware: "plan in 1.1 s" read as the whole wait)
    assert re.fullmatch(r"Done — every step verified · \d+\.\d s", lines[3])
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
    # Said sentence by sentence (D85): the question plays while "Say yes or no." is made.
    assert voice.said[1:3] == ["Should I close zen (workspace 1)?", "Say yes or no."]
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
    assert voice.said == [
        "How can I help?",
        "Done, kitty's on 2.",
        "Hey!",
        "Not much — what can I do for you?",
    ]


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
        # found on real hardware: these went to the planner as requests
        ("Okay, bye.", True),
        ("Okay. Bye.", True),
        ("Bye, bye.", True),
        ("Stop. Bye.", True),
        ("Okay. Okay. Thank you.", True),
        ("No, nothing. Bye. Nothing. Bye.", True),
        ("Thank you, thank you, thank you.", True),
        ("hey ok then bye", True),
        ("thanks sonu, that's all", True),
        ("okay", False),  # no goodbye in it
        ("no", False),
        ("No. I want you to change the volume to 30. That's it.", False),
        ("Thank you, screenshot.", False),
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
        "Should I send the prompt I wrote to Claude Code in todo-app — it's on your screen?",
        "Say yes or no.",
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


@pytest.mark.anyio
async def test_notifications_are_kept_only_when_turned_on(short_tmp: Path, xdg_env: dict[str, str]) -> None:
    from sonu.config.settings import NotificationsSettings

    ollama = FakeOllama()
    off = Daemon(Settings(), SonuPaths.from_env(xdg_env), xdg_env, ollama_transport=ollama.transport)
    assert off.notifications is None
    on = Settings(notifications=NotificationsSettings(remember=True, exclude_apps=["signal"]))
    daemon = Daemon(on, SonuPaths.from_env(xdg_env), xdg_env, ollama_transport=ollama.transport)
    assert daemon.notifications is not None
    assert not daemon.notifications.add("Signal", "Carol", "hi")


@pytest.mark.anyio
async def test_from_the_desktop_the_hud_asks_and_shows(rig: Rig, short_tmp: Path) -> None:
    """D80: with SONU's HUD on, a request with no terminal asks there (buttons), not in the picker, and the
    key's client shows no notification of its own."""
    from sonu.ui.hud import Hud

    hud = Hud(short_tmp / "hud")
    rig.daemon.hud = hud

    async def click_yes() -> None:
        for _ in range(600):  # up to 30 s: a slow CI runner took longer than 5 (Python 3.14, #74)
            await asyncio.sleep(0.05)
            state = json.loads(hud.state_file.read_text()) if hud.state_file.exists() else {}
            if state.get("state") == "asking" and state.get("question", {}).get("options"):
                hud.answer_file.write_text(json.dumps({"id": state["question"]["id"], "value": True}))
                return

    clicking = asyncio.ensure_future(click_yes())
    code, out = await rig.run("--confirm", "gui", "close", "zen")
    await clicking
    assert code == 0 and "✔ Closed zen" in out
    assert not rig.fake.windows_of_class("zen")
    shown = json.loads(hud.state_file.read_text())
    assert shown["state"] == "done" and shown["request"] == "close zen"
    assert shown["steps"] == [{"text": "Closed zen", "status": "done"}]
    assert rig.notices == []  # the HUD showed it: no notification as well


@pytest.mark.anyio
async def test_the_daily_briefing_shows_once_a_day(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """D83: past its time, once; the briefing's content is stubbed (the real one reads this machine)."""
    from datetime import datetime

    from sonu.daemon import server
    from sonu.tools.base import StepStatus, ToolOutcome

    async def fake_brief(ctx: object) -> ToolOutcome:
        return ToolOutcome(StepStatus.DONE, "Good morning — Battery: 76%.", private=True)

    monkeypatch.setattr(server, "brief", fake_brief)
    assert not await rig.daemon.brief_if_due(datetime(2026, 10, 12, 7, 30))  # before 08:00
    assert await rig.daemon.brief_if_due(datetime(2026, 10, 12, 9, 0))
    assert rig.notices == [("SONU — your briefing", "Good morning — Battery: 76%.")]
    assert not await rig.daemon.brief_if_due(datetime(2026, 10, 12, 15, 0))  # shown today
    assert await rig.daemon.brief_if_due(datetime(2026, 10, 13, 8, 1))  # the next day


@pytest.mark.anyio
async def test_a_step_that_takes_a_while_gets_a_one_moment(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """D85: a person says "one moment" instead of going quiet — once, before the reply."""
    monkeypatch.setattr(server, "STILL_WORKING_S", 0.2)
    rig.fake.apps["kitty"].delay_s = 0.8  # kitty takes a while to open
    voice = voice_rig(rig, "open kitty on 2")
    code, out = await rig.run("listen")
    assert code == 0 and "✔ Opened kitty on workspace 2" in out
    assert voice.said == ["How can I help?", "One moment.", "Done, kitty's on 2."]


@pytest.mark.anyio
async def test_a_quick_step_gets_no_one_moment(rig: Rig) -> None:
    voice = voice_rig(rig, "open kitty on 2")
    await rig.run("listen")
    assert "One moment." not in voice.said


@pytest.mark.anyio
async def test_a_heads_up_is_shown_and_said_only_when_the_user_is_free(rig: Rig, tmp_path: Path) -> None:
    """D88: never over a call; said once the microphone is free."""
    from datetime import datetime

    from sonu.config.settings import HeadsUpSettings
    from sonu.core.heads_up import Watch
    from sonu.system import SystemControl
    from sonu.testing.fake_system import FakeSystem

    battery = tmp_path / "power_supply" / "BAT1"
    battery.mkdir(parents=True)
    (battery / "status").write_text("Discharging\n")
    (battery / "capacity").write_text("14\n")
    fake = FakeSystem(mic_users=["Discord"])
    rig.daemon.system = SystemControl(
        fake, power_supply=tmp_path / "power_supply", sys=tmp_path, home=tmp_path
    )
    settings = HeadsUpSettings(enabled=True, speak=True, quiet="", disk_full=100)
    rig.daemon.settings = rig.daemon.settings.model_copy(update={"heads_up": settings})
    rig.daemon.watch = Watch(settings)
    voice = voice_rig(rig)

    [(_, what)] = await rig.daemon.heads_up_now(datetime(2026, 10, 12, 15, 0))
    assert what == "shown only (Discord is using the microphone)" and voice.said == []
    assert rig.notices[-1] == ("SONU", "Battery's at 14% — time to plug in.")
    assert await rig.daemon.heads_up_now(datetime(2026, 10, 12, 15, 1)) == []  # once

    fake.mic_users = []
    (battery / "capacity").write_text("6\n")
    [(_, what)] = await rig.daemon.heads_up_now(datetime(2026, 10, 12, 15, 30))
    assert what == "said"
    assert voice.said == ["Battery's at 6% — plug in now, or the laptop will shut down soon."]


@pytest.mark.anyio
async def test_new_settings_restart_sonud_once_its_idle(rig: Rig) -> None:
    """D90: the Control Center changed a setting — never in the middle of a request."""
    restarted: list[bool] = []
    rig.daemon.restart = lambda: restarted.append(rig.daemon.lock.locked())
    await rig.daemon.lock.acquire()  # a request is running
    reader, writer = await asyncio.open_unix_connection(str(rig.daemon.path))
    writer.write(encode({"type": "reload"}))
    await writer.drain()
    assert decode(await reader.readline()) == {"type": "reloading", "busy": True}
    writer.close()
    await asyncio.sleep(0.1)
    assert restarted == []  # waits for the request
    rig.daemon.lock.release()
    assert rig.daemon._reloading is not None
    await asyncio.wait_for(rig.daemon._reloading, 5)
    assert restarted == [True]  # stopped and started again while holding the lock


@pytest.mark.anyio
async def test_switching_planners_lets_go_of_the_old_one(short_tmp: Path, xdg_env: dict[str, str]) -> None:
    """Found on real hardware: after switching from qwen3.6:35b-a3b, it stayed loaded in the GPU — sonud
    restarts for new settings without unloading. Only a model SONU itself held is let go."""
    ollama = FakeOllama()
    for name in ("qwen3.6:35b-a3b", "qwen3.5:4b"):
        ollama.add_model(name, size_gib=3.2, capabilities=["completion", "tools"])
    ollama.chat_responder = lambda body: json.dumps({"kind": "reply", "message": "Hi!", "steps": []})
    paths = SonuPaths.from_env(xdg_env)

    def daemon_with(model: str) -> Daemon:
        settings = Settings.model_validate({"models": {"planner": {"model": model}}})
        return Daemon(settings, paths, xdg_env, ollama_transport=ollama.transport)

    first = daemon_with("qwen3.6:35b-a3b")
    await first.warm_up()
    await first.stop(unload=False)  # a restart for new settings
    assert ollama.keep_alive == {"qwen3.6:35b-a3b": "-1m"}
    second = daemon_with("qwen3.5:4b")
    await second.warm_up()
    assert ollama.keep_alive == {"qwen3.6:35b-a3b": "0", "qwen3.5:4b": "-1m"}
    await second.stop(unload=False)
    third = daemon_with("qwen3.5:4b")  # the same planner again: nothing let go
    await third.warm_up()
    assert ollama.keep_alive["qwen3.5:4b"] == "-1m"
    await third.stop()


@pytest.mark.anyio
async def test_a_name_heard_another_way_reaches_the_planner_as_the_app(rig: Rig) -> None:
    # Found with the owner's recordings (`sonu voice bench`): Whisper wrote btop as "VTOP" and the plan
    # failed with "'vtop' is not installed". Matched by sound to the installed app (D107), and audited.
    from sonu.audit.log import AuditLog

    voice_rig(rig, "Open VTOP")
    _, out = await rig.run("listen")
    assert "You: Open btop" in out
    asked = [m["messages"][-1]["content"] for m in rig.ollama.chat_requests if "format" in m]
    assert "Open btop" in asked and "Open VTOP" not in asked
    [meant] = [r.data for r in AuditLog(rig.daemon.paths.audit_file).records() if r.kind == "voice.meant"]
    assert meant == {"heard": "Open VTOP", "meant": "Open btop"}


@pytest.mark.anyio
async def test_requests_go_on_while_the_voice_loads(
    short_tmp: Path, xdg_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found on real hardware: Whisper's loading hung on the network after a reinstall, and sonud held every
    request — typed ones too — until it finished, which it never did."""
    ollama = FakeOllama()
    ollama.add_model("qwen3.5:4b", size_gib=3.2, capabilities=["completion", "tools"])
    ollama.chat_responder = lambda body: json.dumps({"kind": "reply", "message": "Hi!", "steps": []})
    daemon = Daemon(Settings(), SonuPaths.from_env(xdg_env), xdg_env, ollama_transport=ollama.transport)
    loading = asyncio.Event()

    class HangingVoice:
        async def warm_up(self, name: str | None = None) -> None:
            loading.set()
            await asyncio.Event().wait()  # never loads

    monkeypatch.setattr(server, "voice_installed", lambda: True)
    monkeypatch.setattr(server, "missing_files", lambda folder: [])
    monkeypatch.setattr(daemon, "voice_session", HangingVoice)
    warming = asyncio.create_task(daemon.warm_up())
    await asyncio.wait_for(loading.wait(), 5)
    assert not daemon.lock.locked()  # a typed request is planned now
    assert daemon._voice_warming.locked()  # the key waits for the voice
    warming.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await warming
    assert not daemon._voice_warming.locked()
    await daemon.stop()


@pytest.mark.anyio
async def test_a_choice_is_said_and_answered_by_voice(rig: Rig) -> None:
    """Found on the laptop: by voice, a choice only showed buttons — "2 packages could be 'cozy' — no answer
    given". SONU says the choices and listens; "the second one" picks VSCodium."""
    rig.fake.apps["codium"] = FakeApp("VSCodium", "Welcome")
    voice = voice_rig(rig, "open vs code", "the second one")
    code, out = await rig.run("listen")
    assert code == 0
    # Said sentence by sentence: the first is always made (the test's speaker exits, so later ones may not be;
    # what the whole list says is tested in test_choosing.py)
    assert "Which app did you mean by 'vs code'?" in voice.said
    assert "✔ Opened VSCodium" in out
    assert rig.fake.windows_of_class("VSCodium")


@pytest.mark.anyio
async def test_an_unclear_answer_is_asked_again_then_left_to_the_buttons(rig: Rig) -> None:
    from sonu.tools.base import Choice, Choices

    voice = voice_rig(rig, "Namaste John.", "")  # "the first one", as turbo wrote it — then nothing
    session = rig.daemon._voice
    assert session is not None
    clicked = asyncio.Event()

    async def buttons(choices: Choices) -> str | None:
        await clicked.wait()
        return "b"

    choose = rig.daemon._voice_chooser(session, buttons, buttons=True)
    asking = asyncio.create_task(choose(Choices("Which one?", (Choice("Apple", "a"), Choice("Banana", "b")))))
    while "I'll leave the choices on the screen." not in voice.said:  # noqa: ASYNC110 — the fake voice's list
        await asyncio.sleep(0.01)
    assert "Sorry, which one?" in voice.said  # asked again (its first sentence)
    clicked.set()  # the buttons kept working: the click is the answer
    assert await asyncio.wait_for(asking, 5) == "b"


@pytest.mark.anyio
async def test_a_spoken_answer_closes_the_buttons(rig: Rig) -> None:
    from sonu.tools.base import Choice, Choices

    voice_rig(rig, "banana")
    session = rig.daemon._voice
    assert session is not None
    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    async def buttons(choices: Choices) -> str | None:
        waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()  # the HUD closes its question when this is cancelled
            raise
        return None

    choose = rig.daemon._voice_chooser(session, buttons, buttons=True)
    picked = await choose(Choices("Which one?", (Choice("Apple", "a"), Choice("Banana", "b"))))
    assert picked == "b" and waiting.is_set() and cancelled.is_set()
