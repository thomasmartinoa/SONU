from __future__ import annotations

import asyncio
import builtins
import io
import json
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.paths import SonuPaths
from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.daemon.client import parse, try_run
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
CLOSE_ZEN = {
    "kind": "steps",
    "message": "",
    "steps": [{"tool": "windows.close", "args": {"window": {"app": "zen"}}}],
}


# ---------------------------------------------------------------- the client's argument parsing
def test_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO())  # not a terminal: questions go to the picker
    assert parse(["open", "kitty", "on", "2"]) == {
        "dry_run": False,
        "json": False,
        "notify": False,
        "confirm": "gui",
        "text": "open kitty on 2",
    }
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
    plans = {"open kitty on 2": LAUNCH_KITTY, "close zen": CLOSE_ZEN}

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
        code = await asyncio.to_thread(try_run, list(argv), out)
        return code, out.getvalue()

    yield Rig(fake_hypr, ollama, run)
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
