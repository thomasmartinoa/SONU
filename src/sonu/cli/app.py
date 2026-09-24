"""The ``sonu`` command."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.markup import escape

from sonu import __version__
from sonu.audit import AuditError, AuditLog
from sonu.cli import audit_cmd, config_cmd, desktop_cmd
from sonu.cli.state import AppState, env_snapshot, get_state
from sonu.doctor import DoctorContext, as_json, render_console, run_checks, summarize
from sonu.logs import setup_logging

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="sonu",
    help="SONU — System-Oriented Neural Utility: a local-first AI operator for your Linux desktop.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
app.add_typer(config_cmd.app, name="config")
app.add_typer(audit_cmd.app, name="audit")
app.add_typer(desktop_cmd.app, name="desktop")


def _version_callback(value: bool) -> None:
    if value:
        print(f"sonu {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="More logging on stderr (-vv for debug)."
    ),
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    state = AppState.load(verbosity=verbose)
    setup_logging(state.paths, verbose)
    ctx.obj = state


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check everything SONU depends on: Hyprland, Ollama, models, GPU, tools, config and audit log."""
    state = get_state(ctx)
    context = DoctorContext(
        env=env_snapshot(),
        paths=state.paths,
        settings=state.settings,
        settings_from_file=state.settings_from_file,
        settings_error=state.settings_error,
        home=Path(env_snapshot().get("HOME") or Path.home()),
    )
    results = asyncio.run(run_checks(context))

    if json_output:
        print(json.dumps(as_json(results, sonu_version=__version__), indent=2, ensure_ascii=False))
    else:
        render_console(state.console, results, sonu_version=__version__)

    counts = summarize(results)
    if state.settings.audit.enabled and not state.settings_error:
        try:
            state.paths.ensure_private_dirs()
            AuditLog(state.paths.audit_file).append("doctor.run", {"summary": counts})
        except (OSError, AuditError) as exc:
            state.err.print(f"[yellow]warning:[/] could not write the audit log: {escape(str(exc))}")
    if counts["fail"]:
        raise typer.Exit(code=1)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
