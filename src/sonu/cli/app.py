"""The ``sonu`` command."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import typer
from rich.markup import escape

from sonu import __version__
from sonu.audit import AuditError, AuditLog
from sonu.cli import (
    ask_cmd,
    audit_cmd,
    browser_cmd,
    config_cmd,
    daemon_cmd,
    desktop_cmd,
    eval_cmd,
    journal_cmd,
    mcp_cmd,
    voice_cmd,
)
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
app.command("ask")(ask_cmd.ask)
app.command("prompt")(ask_cmd.prompt)
app.command("eval")(eval_cmd.eval_cmd)
app.add_typer(config_cmd.app, name="config")
app.add_typer(audit_cmd.app, name="audit")
app.add_typer(desktop_cmd.app, name="desktop")
app.add_typer(daemon_cmd.app, name="daemon")
app.add_typer(voice_cmd.app, name="voice")
app.add_typer(journal_cmd.app, name="journal")
app.add_typer(mcp_cmd.app, name="mcp")
app.add_typer(browser_cmd.app, name="browser")


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


COMMANDS = {
    "ask",
    "prompt",
    "eval",
    "doctor",
    "config",
    "audit",
    "desktop",
    "daemon",
    "voice",
    "listen",
    "journal",
    "mcp",
    "browser",
}
_GLOBAL_FLAGS = {"-v", "-vv", "-vvv", "--verbose"}
# `sonu ask` options that may come before a bare request: `sonu --dry-run open kitty`.
_ASK_FLAGS = {"--dry-run", "--deep", "--json", "--notify"}
_ASK_OPTIONS = {"--model", "--confirm"}  # these take a value


def rewrite_bare_request(args: list[str]) -> list[str]:
    """`sonu open kitty on 3` → `sonu ask open kitty on 3`.

    Global flags (-v) and `ask` options (--dry-run, --model X, …) may come before the request.
    """
    global_flags: list[str] = []
    ask_options: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _GLOBAL_FLAGS:
            global_flags.append(arg)
        elif arg in _ASK_FLAGS or (arg.partition("=")[0] in _ASK_OPTIONS and "=" in arg):
            ask_options.append(arg)
        elif arg in _ASK_OPTIONS and index + 1 < len(args):
            ask_options += args[index : index + 2]
            index += 1
        else:
            break
        index += 1
    if index < len(args) and not args[index].startswith("-") and args[index] not in COMMANDS:
        return [*global_flags, "ask", *ask_options, *args[index:]]
    return args


@app.command("listen")
def listen() -> None:
    """Talk to SONU (the SONU key runs this). Needs the daemon: it keeps the voice models loaded."""
    typer.echo("✖ Voice needs the SONU daemon: `sonu daemon install`, then `sonu voice setup`", err=True)
    raise typer.Exit(code=1)


def main() -> None:
    sys.argv = [sys.argv[0], *rewrite_bare_request(sys.argv[1:])]
    try:
        app()
    except KeyboardInterrupt:
        # Say so: a step that was already sent (e.g. a page opening) may still complete.
        print(
            "\n✖ Stopped (Ctrl+C). A step already sent to an app may still finish — "
            "`sonu audit tail` shows what was done.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
