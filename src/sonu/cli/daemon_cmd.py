"""`sonu daemon …`: run SONU in the background so requests answer in about a second."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import typer

from sonu.cli.state import env_snapshot, get_state
from sonu.daemon.protocol import socket_path
from sonu.llm.cloud import CloudRefused, check_local_roles

app = typer.Typer(
    help="SONU in the background: the planner model stays loaded, requests answer in about a second.",
    no_args_is_help=True,
)

UNIT_NAME = "sonud.service"


def unit_text(executable: str) -> str:
    """A systemd user unit tied to the graphical session (uwsm starts and stops it with Hyprland)."""
    return f"""[Unit]
Description=SONU daemon (local AI operator for the desktop)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={executable} daemon run
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""


def _unit_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "systemd" / "user" / UNIT_NAME


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


@app.command("run")
def daemon_run(ctx: typer.Context) -> None:
    """Run the daemon in the foreground (systemd runs this)."""
    from sonu.daemon.server import run_daemon

    state = get_state(ctx)
    state.require_valid_config()
    try:
        check_local_roles(state.settings)
    except CloudRefused as exc:
        typer.echo(f"✖ {exc}", err=True)
        raise typer.Exit(code=1) from exc

    async def main() -> None:
        task = asyncio.create_task(run_daemon(state.settings, state.paths, env_snapshot()))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):  # SIGTERM from systemd: stop cleanly
            await task

    asyncio.run(main())


@app.command("install")
def daemon_install() -> None:
    """Install and start the daemon as a systemd user service (starts with your Hyprland session)."""
    executable = shutil.which("sonu") or sys.argv[0]
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(executable))
    for args in (("daemon-reload",), ("enable", "--now", UNIT_NAME), ("restart", UNIT_NAME)):
        result = _systemctl(*args)
        if result.returncode != 0:
            typer.echo(f"✖ systemctl --user {' '.join(args)}: {result.stderr.strip()}", err=True)
            raise typer.Exit(code=1)
    typer.echo(f"✔ Installed {path} and started it — `sonu daemon status` shows it")


@app.command("uninstall")
def daemon_uninstall() -> None:
    """Stop the daemon and remove its systemd user service."""
    _systemctl("disable", "--now", UNIT_NAME)
    _unit_path().unlink(missing_ok=True)
    _systemctl("daemon-reload")
    typer.echo("✔ The daemon is stopped and removed; `sonu` runs requests by itself again")


@app.command("status")
def daemon_status() -> None:
    """Is the daemon running?"""
    active = _systemctl("is-active", UNIT_NAME).stdout.strip()
    listening = socket_path().exists()
    typer.echo(f"service: {active or 'not installed'} · socket: {socket_path() if listening else 'none'}")
    if active != "active":
        raise typer.Exit(code=1)
