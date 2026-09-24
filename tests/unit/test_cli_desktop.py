from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.cli.app import app
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from tests.conftest import ThreadedFakeHyprland

TITLE = "main.py - sonu - Code - OSS"


@pytest.fixture
def env(xdg_env: dict[str, str], threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]) -> dict[str, str]:
    _, fake = threaded_hypr
    fake.windows = [
        make_window("0xa1", "code-oss", TITLE, workspace_id=3),
        make_window("0xb2", "chromium", "New Tab - Chromium", workspace_id=2),
    ]
    config = Path(xdg_env["XDG_CONFIG_HOME"]) / "sonu" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[ollama]\nhost = "http://127.0.0.1:9"\ntimeout_s = 1.0\n')
    return xdg_env | fake.env | {"WAYLAND_DISPLAY": "wayland-1"}


def test_windows_table_and_privacy(env: dict[str, str]) -> None:
    runner = CliRunner()
    shown = runner.invoke(app, ["desktop", "windows"], env=env)
    assert shown.exit_code == 0, shown.output
    assert "code-oss" in shown.output
    assert "sonu - Code" in shown.output

    hidden = runner.invoke(app, ["desktop", "windows", "--no-titles"], env=env)
    assert hidden.exit_code == 0
    assert "code-oss" in hidden.output
    assert "sonu - Code" not in hidden.output


def test_windows_json(env: dict[str, str]) -> None:
    result = CliRunner().invoke(app, ["desktop", "windows", "--json", "--no-titles"], env=env)
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [(row["workspace"], row["class"], row["title"]) for row in rows] == [
        ("2", "chromium", None),
        ("3", "code-oss", None),
    ]


def test_workspaces_and_monitors(env: dict[str, str]) -> None:
    runner = CliRunner()
    workspaces = runner.invoke(app, ["desktop", "workspaces"], env=env)
    assert workspaces.exit_code == 0, workspaces.output
    assert "eDP-1" in workspaces.output
    monitors = runner.invoke(app, ["desktop", "monitors"], env=env)
    assert monitors.exit_code == 0, monitors.output
    assert "2560x1440@165" in monitors.output


def test_events_stream(env: dict[str, str], threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]) -> None:
    runner_thread, fake = threaded_hypr

    async def emit_when_connected() -> None:
        await fake.wait_for_event_clients(1, timeout_s=5)
        await fake.emit("openwindow", f"a1,3,code-oss,{TITLE}")
        await fake.emit("movewindowv2", "a1,2,2")

    pending = runner_thread.submit(emit_when_connected())
    result = CliRunner().invoke(app, ["desktop", "events", "--count", "2", "--no-titles"], env=env)
    pending.result(timeout=5)
    assert result.exit_code == 0, result.output
    assert "openwindow" in result.output
    assert "movewindowv2" in result.output
    assert "title hidden" in result.output
    assert "sonu - Code" not in result.output


def test_doctor_human_output(env: dict[str, str]) -> None:
    result = CliRunner().invoke(app, ["doctor"], env=env)
    # Ollama is unreachable in this environment, so the doctor reports a failure.
    assert result.exit_code == 1
    assert "SONU doctor" in result.output
    assert "Hyprland 0.56.2" in result.output
    assert "lua config" in result.output
    assert "sonu - Code" not in result.output
