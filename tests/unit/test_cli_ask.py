from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sonu.cli import ask_cmd
from sonu.cli.app import app, rewrite_bare_request
from sonu.runtime import build_runtime
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_ollama import FakeOllama
from sonu.ui.desktop_ui import Picker
from tests.conftest import ThreadedFakeHyprland

LAUNCH_KITTY = {
    "kind": "steps",
    "message": "Opening kitty on workspace 2.",
    "steps": [{"tool": "apps.launch", "args": {"app": "kitty", "workspace": 2}}],
}
CLOSE_KITTY = {
    "kind": "steps",
    "message": "Closing kitty.",
    "steps": [{"tool": "windows.close", "args": {"window": {"app": "kitty"}}}],
}


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    fake = FakeOllama()
    fake.add_model("qwen3.5:4b", size_gib=3.2, capabilities=["completion", "tools", "thinking"])
    fake.chat_responder = lambda body: json.dumps(LAUNCH_KITTY)
    monkeypatch.setattr(
        ask_cmd, "build_runtime", functools.partial(build_runtime, ollama_transport=fake.transport)
    )
    return fake


@pytest.fixture
def env(
    xdg_env: dict[str, str], tmp_path: Path, threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]
) -> dict[str, str]:
    _, fake = threaded_hypr
    fake.apps["kitty"] = FakeApp("kitty", "zsh")
    write_desktop_files(Path(xdg_env["XDG_DATA_HOME"]) / "applications")
    config = Path(xdg_env["XDG_CONFIG_HOME"]) / "sonu" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[apps]\nlaunch_timeout_s = 3.0\n[verify]\ntimeout_s = 2.0\n")
    return xdg_env | fake.env | {"XDG_DATA_DIRS": str(tmp_path / "none"), "WAYLAND_DISPLAY": "wayland-1"}


def invoke(args: list[str], env: dict[str, str]) -> Any:
    return CliRunner().invoke(app, args, env=env)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["open", "kitty"], ["ask", "open", "kitty"]),
        (["-v", "open", "kitty"], ["-v", "ask", "open", "kitty"]),
        (["doctor"], ["doctor"]),
        (["ask", "open", "kitty"], ["ask", "open", "kitty"]),
        (["--help"], ["--help"]),
        ([], []),
    ],
)
def test_bare_requests_become_ask(argv: list[str], expected: list[str]) -> None:
    assert rewrite_bare_request(argv) == expected


def test_ask_opens_kitty_and_reports_verified_result(
    env: dict[str, str], fake_ollama: FakeOllama, threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]
) -> None:
    _, fake = threaded_hypr
    result = invoke(["ask", "open", "kitty", "on", "workspace", "2"], env)
    assert result.exit_code == 0, result.output
    assert "✔ Opened kitty on workspace 2" in result.output
    assert "Done — every step verified" in result.output
    assert [w["workspace"]["id"] for w in fake.windows_of_class("kitty")] == [2]
    assert fake_ollama.chat_requests[0]["messages"][-1]["content"] == "open kitty on workspace 2"


def test_dry_run_changes_nothing(
    env: dict[str, str], fake_ollama: FakeOllama, threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]
) -> None:
    _, fake = threaded_hypr
    result = invoke(["ask", "--dry-run", "open kitty on 2"], env)
    assert result.exit_code == 0, result.output
    assert "would: Open kitty on workspace 2" in result.output
    assert fake.launched == []


def test_json_report(env: dict[str, str], fake_ollama: FakeOllama) -> None:
    result = invoke(["ask", "--json", "open kitty on 2"], env)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "done"
    assert report["steps"][0]["checks"][0]["passed"] is True
    assert report["plan"]["model"] == "qwen3.5:4b"


def test_risky_step_is_skipped_when_nobody_can_confirm(
    env: dict[str, str], fake_ollama: FakeOllama, threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]
) -> None:
    _, fake = threaded_hypr
    fake.windows = [make_window("0xa1", "kitty", "zsh", stable_id="a1")]
    fake_ollama.chat_responder = lambda body: json.dumps(CLOSE_KITTY)
    result = invoke(["ask", "--confirm", "never", "close kitty"], env)
    assert result.exit_code == 1
    assert "Skipped (not approved)" in result.output
    assert len(fake.windows) == 1


def test_terminal_confirmation_yes(
    env: dict[str, str], fake_ollama: FakeOllama, threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]
) -> None:
    _, fake = threaded_hypr
    fake.windows = [make_window("0xa1", "kitty", "zsh", stable_id="a1")]
    fake_ollama.chat_responder = lambda body: json.dumps(CLOSE_KITTY)
    result = CliRunner().invoke(app, ["ask", "--confirm", "terminal", "close kitty"], env=env, input="y\n")
    assert result.exit_code == 0, result.output
    assert "SONU wants to" in result.output
    assert fake.windows == []


def test_without_hyprland(xdg_env: dict[str, str], fake_ollama: FakeOllama, tmp_path: Path) -> None:
    env = xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path), "HYPRLAND_INSTANCE_SIGNATURE": ""}
    result = invoke(["ask", "open kitty"], env)
    assert result.exit_code == 1
    assert "inside your Hyprland session" in result.output


def test_invalid_config_blocks_asking(env: dict[str, str], fake_ollama: FakeOllama) -> None:
    (Path(env["XDG_CONFIG_HOME"]) / "sonu" / "config.toml").write_text('[policy]\nautonomy = "yolo"\n')
    result = invoke(["ask", "open kitty"], env)
    assert result.exit_code == 2


def test_prompt_needs_a_picker(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Picker, "detect", classmethod(lambda cls, which=None: None))
    result = invoke(["prompt"], env)
    assert result.exit_code == 1
    assert "needs rofi, fuzzel or wofi" in result.output
