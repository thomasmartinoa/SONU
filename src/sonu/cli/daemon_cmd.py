"""`sonu daemon …`: run SONU in the background so requests answer in about a second.

`sonu on` / `sonu off` (D110) are one switch for all of it. Off: sonud stopped, SONU's models out of the GPU,
not started at login, and the SONU key and typed requests say SONU is off instead of loading the model
themselves. On: started now and at every login.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import typer

from sonu.audit import AuditError, AuditLog
from sonu.cli.state import AppState, env_snapshot, get_state
from sonu.config import Settings, SonuPaths
from sonu.daemon.protocol import socket_path
from sonu.llm.cloud import CloudRefused, check_local_roles
from sonu.llm.ollama import OllamaClient, OllamaError, same_model

app = typer.Typer(
    help="SONU in the background: the planner model stays loaded, requests answer in about a second.",
    no_args_is_help=True,
)

UNIT_NAME = "sonud.service"
UNLOAD_WAIT_S = 5.0  # Ollama lets a model go a moment after it's asked to
START_WAIT_S = 10.0  # for sonud's socket after `sonu on`


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
    argv = ["systemctl", "--user", *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:  # no systemd
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _write_unit() -> Path:
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(shutil.which("sonu") or sys.argv[0]))
    return path


@dataclass(frozen=True, slots=True)
class Power:
    on: bool  # the user hasn't turned SONU off
    installed: bool  # sonud's unit file is there
    running: bool
    at_login: bool  # enabled: starts with the Hyprland session


def power(paths: SonuPaths) -> Power:
    return Power(
        on=not paths.off_file.exists(),
        installed=_unit_path().exists(),
        running=_systemctl("is-active", UNIT_NAME).stdout.strip() == "active",
        at_login=_systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled",
    )


def stop_if_off(state: AppState, *, desktop: bool = False) -> None:
    """A request while SONU is off: say so (on the desktop too, from the SONU key) — never load the model."""
    if not state.paths.off_file.exists():
        return
    if desktop:
        from sonu.daemon.client import Notice

        Notice().show("SONU is off", "Turn it on in the Control Center, or with `sonu on`.")
    state.err.print("[yellow]SONU is off[/] — `sonu on` (or the switch in the Control Center) turns it on.")
    raise typer.Exit(code=1)


def ollama_client(settings: Settings) -> OllamaClient:  # tests swap in a fake Ollama
    return OllamaClient(settings.ollama.host, timeout_s=settings.ollama.timeout_s)


def _sonus_models(settings: Settings, paths: SonuPaths) -> set[str]:
    """SONU's own models: the ones it's set to use, and the one sonud noted it held — never another."""
    models = settings.models
    names = {models.planner.model, models.planner_deep.model, models.embed.model}
    with contextlib.suppress(OSError):
        names.add((paths.state_dir / "held-model").read_text(encoding="utf-8").strip())
    return {name for name in names if name}


async def _loaded(client: OllamaClient, names: set[str]) -> list[str]:
    return [m.name for m in await client.loaded_models() if any(same_model(m.name, n) for n in names)]


async def _sonus_loaded(settings: Settings, paths: SonuPaths) -> list[str]:
    """Which of SONU's models Ollama holds now ([] when Ollama isn't running: nothing is loaded)."""
    async with ollama_client(settings) as client:
        try:
            return await _loaded(client, _sonus_models(settings, paths))
        except OllamaError:
            return []


async def _let_go(settings: Settings, paths: SonuPaths) -> list[str]:
    """Unload SONU's models sonud didn't (e.g. one a request without sonud loaded); the ones still loaded."""
    names = _sonus_models(settings, paths)
    async with ollama_client(settings) as client:
        try:
            for name in await _loaded(client, names):
                await client.load(name, keep_alive="0")
            deadline = time.monotonic() + UNLOAD_WAIT_S
            while True:
                still = await _loaded(client, names)
                if not still or time.monotonic() > deadline:
                    return still
                await asyncio.sleep(0.25)
        except OllamaError:
            with contextlib.suppress(OllamaError):
                return await _loaded(client, names)
            return []  # Ollama stopped: nothing is loaded


def _audit(state: AppState, kind: str, data: dict[str, object]) -> None:
    if not state.settings.audit.enabled:
        return
    with contextlib.suppress(OSError, AuditError):
        state.paths.ensure_private_dirs()
        AuditLog(state.paths.audit_file).append(kind, data)


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
def daemon_install(ctx: typer.Context) -> None:
    """Install and start the daemon as a systemd user service (starts with your Hyprland session)."""
    get_state(ctx).paths.off_file.unlink(missing_ok=True)  # installing it turns SONU on
    path = _write_unit()
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
def daemon_status(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="On, running, starts at login (the Control Center)."),
) -> None:
    """Is the daemon running?"""
    if as_json:
        typer.echo(json.dumps(asdict(power(get_state(ctx).paths))))
        return
    active = _systemctl("is-active", UNIT_NAME).stdout.strip()
    listening = socket_path().exists()
    typer.echo(f"service: {active or 'not installed'} · socket: {socket_path() if listening else 'none'}")
    if get_state(ctx).paths.off_file.exists():
        typer.echo("SONU is off — `sonu on` turns it on")
    if active != "active":
        raise typer.Exit(code=1)


def turn_off(ctx: typer.Context) -> None:
    """Turn SONU off: stopped now, its models out of the GPU, and not started at login (`sonu on`: back)."""
    state = get_state(ctx)
    paths = state.paths
    paths.ensure_private_dirs()
    paths.off_file.write_text("off\n")  # first: from now on a request doesn't load the model by itself
    held = asyncio.run(_sonus_loaded(state.settings, paths))
    problems: list[str] = []
    if _unit_path().exists():
        stopped = _systemctl("disable", "--now", UNIT_NAME)  # sonud unloads its planner as it stops
        if stopped.returncode != 0:
            problems.append(f"systemctl --user disable --now {UNIT_NAME}: {stopped.stderr.strip()}")
    still = asyncio.run(_let_go(state.settings, paths))
    now = power(paths)  # checked afresh, not taken from systemctl's exit code
    if now.running:
        problems.append("sonud is still running — `systemctl --user status sonud` says why")
    if now.at_login:
        problems.append("it would still start at login")
    if still:
        problems.append(f"{', '.join(still)} is still loaded in Ollama")
    _audit(state, "sonu.off", {"unloaded": [n for n in held if n not in still], "problems": problems})
    if problems:
        for problem in problems:
            typer.echo(f"✖ {problem}", err=True)
        raise typer.Exit(code=1)
    models = f"{', '.join(held)} unloaded from the GPU" if held else "no model of its own loaded"
    typer.echo(f"✔ SONU is off: stopped, {models}, and it won't start at login.")
    typer.echo(
        "  The SONU key and requests say it's off until `sonu on` (or the switch in the Control Center)."
    )


def turn_on(ctx: typer.Context) -> None:
    """Turn SONU on: started now and at every login (the planner loads in a few seconds)."""
    state = get_state(ctx)
    state.require_valid_config()
    state.paths.off_file.unlink(missing_ok=True)
    steps: list[tuple[str, ...]] = [("enable", "--now", UNIT_NAME)]
    if not _unit_path().exists():
        _write_unit()
        steps.insert(0, ("daemon-reload",))
    for args in steps:
        result = _systemctl(*args)
        if result.returncode != 0:
            typer.echo(f"✖ systemctl --user {' '.join(args)}: {result.stderr.strip()}", err=True)
            raise typer.Exit(code=1)
    deadline = time.monotonic() + START_WAIT_S
    while not socket_path().exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    now = power(state.paths)
    _audit(state, "sonu.on", {"running": now.running, "at_login": now.at_login})
    if not now.running:
        typer.echo("✖ sonud didn't start — `journalctl --user -u sonud -n 30` says why", err=True)
        raise typer.Exit(code=1)
    if not now.at_login:
        typer.echo(
            "✖ SONU is running, but it won't start at login (`systemctl --user enable sonud`)", err=True
        )
        raise typer.Exit(code=1)
    ready = "" if socket_path().exists() else " (still getting ready)"
    typer.echo(f"✔ SONU is on{ready}, and it starts at every login. The planner loads in a few seconds.")
