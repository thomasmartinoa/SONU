"""`sonu on` / `sonu off` (D110): one switch — off is stopped, unloaded and not started at login."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.cli import daemon_cmd
from sonu.cli.app import app
from sonu.daemon.client import Notice, parse
from sonu.llm.ollama import OllamaClient
from sonu.testing.fake_ollama import FakeOllama


@dataclass
class FakeSystemd:
    """``systemctl --user`` for sonud.service: is it enabled, is it running."""

    ollama: FakeOllama
    enabled: bool = True
    active: bool = True
    refuse: bool = False  # stopping fails, as when the unit hangs
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        out, code = "", 0
        if args[0] == "is-active":
            out = "active" if self.active else "inactive"
        elif args[0] == "is-enabled":
            out = "enabled" if self.enabled else "disabled"
        elif args[:2] == ("disable", "--now"):
            if self.refuse:
                return subprocess.CompletedProcess(args, 1, "", "Job for sonud.service canceled")
            self.enabled = self.active = False
            # sonud unloads its planner as it stops (Daemon.stop)
            self.ollama.loaded = [m for m in self.ollama.loaded if m["name"] != "qwen3.5:4b"]
        elif args[:2] == ("enable", "--now"):
            self.enabled = self.active = True
        return subprocess.CompletedProcess(args, code, out + "\n", "")


@pytest.fixture
def env(xdg_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "run"
    runtime.mkdir()
    return xdg_env | {"XDG_RUNTIME_DIR": str(runtime)}  # never the real sonud's socket


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    fake = FakeOllama()
    fake.add_model("qwen3.5:4b", size_gib=3.4, capabilities=["completion", "tools"])
    fake.add_model("llama3.2:3b", size_gib=2.0, capabilities=["completion"])  # the user's own
    monkeypatch.setattr(daemon_cmd, "ollama_client", lambda settings: OllamaClient(transport=fake.transport))
    return fake


@pytest.fixture
def systemd(monkeypatch: pytest.MonkeyPatch, ollama: FakeOllama, env: dict[str, str]) -> FakeSystemd:
    fake = FakeSystemd(ollama)
    monkeypatch.setattr(daemon_cmd, "_systemctl", fake)
    monkeypatch.setattr(daemon_cmd, "UNLOAD_WAIT_S", 0.0)
    monkeypatch.setattr(daemon_cmd, "START_WAIT_S", 0.0)
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / daemon_cmd.UNIT_NAME
    unit.parent.mkdir(parents=True)
    unit.write_text(daemon_cmd.unit_text("/usr/bin/sonu"))
    return fake


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []  # never a real notify-send on the desktop running the tests
    monkeypatch.setattr(Notice, "show", lambda self, title, body="", **_: shown.append(title))
    return shown


def _off(env: dict[str, str]) -> Path:
    return Path(env["XDG_STATE_HOME"]) / "sonu" / "off"


def test_off_stops_sonu_unloads_its_model_and_keeps_it_off_at_login(
    env: dict[str, str], systemd: FakeSystemd, ollama: FakeOllama
) -> None:
    ollama.load("qwen3.5:4b", vram_gib=3.4)
    ollama.load("llama3.2:3b", vram_gib=2.0)
    result = CliRunner().invoke(app, ["off"], env=env)
    assert result.exit_code == 0, result.output
    assert ("disable", "--now", "sonud.service") in systemd.calls
    assert not systemd.active and not systemd.enabled
    assert [m["name"] for m in ollama.loaded] == ["llama3.2:3b"]  # the user's own model is left alone
    assert _off(env).exists()
    assert "SONU is off" in result.output and "qwen3.5:4b unloaded" in result.output
    assert "won't start at login" in result.output


def test_off_unloads_the_model_a_request_without_sonud_loaded(
    env: dict[str, str], systemd: FakeSystemd, ollama: FakeOllama
) -> None:
    systemd.active = False  # sonud wasn't running; a request ran by itself and left the planner loaded
    ollama.load("qwen3.5:4b", vram_gib=3.4)
    ollama.loaded[0]["name"] = ollama.loaded[0]["model"] = "qwen3.5:4b-q4"  # not this exact name in /api/ps
    ollama.models["qwen3.5:4b-q4"] = ollama.models["qwen3.5:4b"]
    held = Path(env["XDG_STATE_HOME"]) / "sonu" / "held-model"
    held.parent.mkdir(parents=True)
    held.write_text("qwen3.5:4b-q4\n")  # the model sonud noted it held
    result = CliRunner().invoke(app, ["off"], env=env)
    assert result.exit_code == 0, result.output
    assert ollama.loaded == [] and ollama.keep_alive["qwen3.5:4b-q4"] == "0"


def test_off_is_not_claimed_when_sonud_keeps_running(env: dict[str, str], systemd: FakeSystemd) -> None:
    systemd.refuse = True
    result = CliRunner().invoke(app, ["off"], env=env)
    assert result.exit_code == 1
    assert "still running" in result.output and "still start at login" in result.output
    assert "SONU is off:" not in result.output


def test_while_off_requests_and_the_key_say_so_instead_of_loading_the_model(
    env: dict[str, str], systemd: FakeSystemd, ollama: FakeOllama, notices: list[str]
) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["off"], env=env).exit_code == 0
    asked = runner.invoke(app, ["ask", "open", "kitty"], env=env)
    assert asked.exit_code == 1 and "SONU is off" in asked.output
    assert ollama.chat_requests == []
    key = runner.invoke(app, ["listen"], env=env)  # the SONU key: no terminal, so on the desktop
    assert key.exit_code == 1 and "SONU is off" in key.output
    assert notices == ["SONU is off"]


def test_on_starts_sonu_now_and_at_login(env: dict[str, str], systemd: FakeSystemd) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["off"], env=env).exit_code == 0
    result = runner.invoke(app, ["on"], env=env)
    assert result.exit_code == 0, result.output
    assert ("enable", "--now", "sonud.service") in systemd.calls
    assert systemd.active and systemd.enabled and not _off(env).exists()
    assert "SONU is on" in result.output and "every login" in result.output
    status = runner.invoke(app, ["daemon", "status", "--json"], env=env)
    assert json.loads(status.output) == {"on": True, "installed": True, "running": True, "at_login": True}


def test_on_installs_the_service_when_it_isnt_there(env: dict[str, str], systemd: FakeSystemd) -> None:
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / daemon_cmd.UNIT_NAME
    unit.unlink()
    systemd.enabled = systemd.active = False
    result = CliRunner().invoke(app, ["on"], env=env)
    assert result.exit_code == 0, result.output
    assert unit.exists() and systemd.calls[0] == ("daemon-reload",)


def test_on_and_off_are_commands_not_requests_for_the_daemon() -> None:
    assert parse(["off"]) is None and parse(["on"]) is None
