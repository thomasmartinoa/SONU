from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu import __version__
from sonu.cli.app import app
from sonu.config import SonuPaths


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env(xdg_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    # No Hyprland here, and point Ollama at a closed port so tests never touch a real server.
    runtime = tmp_path / "run"
    runtime.mkdir()
    config = Path(xdg_env["XDG_CONFIG_HOME"]) / "sonu" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[ollama]\nhost = "http://127.0.0.1:9"\ntimeout_s = 1.0\n')
    return xdg_env | {
        "XDG_RUNTIME_DIR": str(runtime),
        "HYPRLAND_INSTANCE_SIGNATURE": "",
        "WAYLAND_DISPLAY": "",
    }


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"sonu {__version__}"


def test_config_init_writes_a_private_file_once(runner: CliRunner, xdg_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["config", "init"], env=xdg_env)
    assert result.exit_code == 0, result.output
    path = SonuPaths.from_env(xdg_env).config_file
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    again = runner.invoke(app, ["config", "init"], env=xdg_env)
    assert again.exit_code == 1

    forced = runner.invoke(app, ["config", "init", "--force"], env=xdg_env)
    assert forced.exit_code == 0
    assert list(path.parent.glob("config.toml.bak-*"))


def test_config_show_prints_effective_settings(runner: CliRunner, env: dict[str, str]) -> None:
    result = runner.invoke(app, ["config", "show"], env=env)
    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert shown["ollama"]["host"] == "http://127.0.0.1:9"
    assert shown["policy"]["autonomy"] == "confirm-risky"


def test_invalid_config_blocks_everything_but_doctor_and_config(
    runner: CliRunner, env: dict[str, str]
) -> None:
    config = SonuPaths.from_env(env).config_file
    config.write_text('[policy]\nautonomy = "yolo"\n')
    blocked = runner.invoke(app, ["audit", "tail"], env=env)
    assert blocked.exit_code == 2
    assert "policy.autonomy" in blocked.output
    assert runner.invoke(app, ["config", "path"], env=env).exit_code == 0


def test_doctor_json_without_hyprland_or_ollama(runner: CliRunner, env: dict[str, str]) -> None:
    result = runner.invoke(app, ["doctor", "--json"], env=env)
    assert result.exit_code == 1  # failures present
    report = json.loads(result.stdout)
    statuses = {check["id"]: check["status"] for check in report["checks"]}
    assert statuses["hyprland.instance"] == "fail"
    assert statuses["ollama.server"] == "fail"
    assert report["summary"]["fail"] >= 2


def test_doctor_is_audited(runner: CliRunner, env: dict[str, str]) -> None:
    runner.invoke(app, ["doctor", "--json"], env=env)
    tail = runner.invoke(app, ["audit", "tail", "--json"], env=env)
    assert tail.exit_code == 0
    records = [json.loads(line) for line in tail.stdout.splitlines()]
    assert records[-1]["kind"] == "doctor.run"
    verify = runner.invoke(app, ["audit", "verify"], env=env)
    assert verify.exit_code == 0
    assert "hash chain intact" in verify.output


def test_desktop_commands_without_hyprland_fail_cleanly(runner: CliRunner, env: dict[str, str]) -> None:
    result = runner.invoke(app, ["desktop", "windows"], env=env)
    assert result.exit_code == 1
    assert "Hyprland does not seem to be running" in result.output


def test_every_subcommand_is_routed_as_a_command_not_a_request() -> None:
    # Found on the laptop: `sonu journal show today` went to the planner as a request.
    from sonu.cli.app import COMMANDS, rewrite_bare_request
    from sonu.daemon.client import _COMMANDS

    names = {group.name for group in app.registered_groups} | {
        command.name or (command.callback.__name__ if command.callback else "")
        for command in app.registered_commands
    }
    names.discard("version")
    assert names <= COMMANDS
    assert names - {"listen"} <= _COMMANDS  # the daemon's client leaves them to the full CLI
    assert rewrite_bare_request(["journal", "show", "today"]) == ["journal", "show", "today"]


def test_the_journal_command(runner: CliRunner, env: dict[str, str]) -> None:
    from sonu.core.journal import Journal

    data = Path(env["XDG_DATA_HOME"]) / "sonu"
    journal = Journal(data / "journal.sqlite3")
    journal.record("kitty", "nvim dataset.py", 1.0e9)  # 2001: older than 30 days, but forget --all takes it
    journal.close()
    path = runner.invoke(app, ["journal", "path"], env=env).output.replace("\n", "")
    assert path == str(data / "journal.sqlite3")
    result = runner.invoke(app, ["journal", "show", "today"], env=env)
    assert result.exit_code == 0 and "Nothing in the journal today" in result.output
    assert runner.invoke(app, ["journal", "show", "tomorrow"], env=env).exit_code == 2
    assert runner.invoke(app, ["journal", "pause"], env=env).exit_code == 0
    assert (data / "journal-paused").exists()
    assert "paused" in runner.invoke(app, ["journal", "show"], env=env).output
    assert runner.invoke(app, ["journal", "resume"], env=env).exit_code == 0
    assert not (data / "journal-paused").exists()
    result = runner.invoke(app, ["journal", "forget", "--all"], env=env)
    assert result.exit_code == 0 and "Deleted 1 entries." in result.output
