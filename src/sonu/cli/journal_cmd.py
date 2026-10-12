from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import get_args

import typer
from rich.markup import escape

from sonu.cli.state import AppState, get_state
from sonu.core.journal import Journal
from sonu.desktop.apps.catalog import AppCatalog
from sonu.runtime import open_journal
from sonu.tools.journal_tools import AppNames, When, period, summary

app = typer.Typer(
    help="The activity journal: which window had the focus and when (local only; sonud records it).",
    no_args_is_help=True,
)


def _journal(state: AppState) -> Journal:
    state.require_valid_config()
    journal = open_journal(state.settings, state.paths)
    if journal is None:
        state.err.print("The activity journal is off ([journal] enabled = false in the config).")
        raise typer.Exit(code=1)
    return journal


@app.command("show")
def journal_show(
    ctx: typer.Context,
    when: str = typer.Argument("today", help=f"one of: {', '.join(get_args(When))}"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Control Center)."),
) -> None:
    """Sum up the windows you used (today by default)."""
    state = get_state(ctx)
    if when not in get_args(When):
        state.err.print(f"[red]error:[/] when must be one of: {', '.join(get_args(When))}")
        raise typer.Exit(code=2)
    journal = _journal(state)
    now = datetime.now()
    start, end, said = period(when, now)  # type: ignore[arg-type]  # checked above
    spans = journal.spans(start.timestamp(), end.timestamp(), now.timestamp())
    if as_json:
        said_text = summary(spans, AppNames(AppCatalog.load(os.environ).entries), said) if spans else ""
        print(
            json.dumps({"summary": said_text or f"Nothing in the journal {said}", "paused": journal.paused})
        )
        journal.close()
        return
    if not spans:
        state.console.print(f"Nothing in the journal {said}")
    else:
        names = AppNames(AppCatalog.load(os.environ).entries)
        state.console.print(escape(summary(spans, names, said)), highlight=False)
    if journal.paused:
        state.console.print("The journal is paused — `sonu journal resume` to record again.")
    journal.close()


@app.command("pause")
def journal_pause(ctx: typer.Context) -> None:
    """Stop recording until `sonu journal resume`."""
    state = get_state(ctx)
    journal = _journal(state)
    journal.pause(True)
    journal.close()
    state.console.print("The journal is paused.")


@app.command("resume")
def journal_resume(ctx: typer.Context) -> None:
    """Record again (from the next window you switch to)."""
    state = get_state(ctx)
    journal = _journal(state)
    journal.pause(False)
    journal.close()
    state.console.print("The journal is recording again.")


@app.command("forget")
def journal_forget(
    ctx: typer.Context,
    hours: float = typer.Option(None, "--hours", min=0.01, help="Delete the last this many hours."),
    everything: bool = typer.Option(False, "--all", help="Delete the whole journal."),
) -> None:
    """Delete the last hours of the journal, or all of it."""
    state = get_state(ctx)
    if (hours is None) == (not everything):
        state.err.print("[red]error:[/] give --hours N or --all")
        raise typer.Exit(code=2)
    journal = _journal(state)
    since = 0.0 if everything else (datetime.now() - timedelta(hours=hours)).timestamp()
    count = journal.count(since)
    journal.forget(since)
    left = journal.count(since)
    journal.close()
    if left:
        state.err.print(f"[red]error:[/] {left} entries are still there")
        raise typer.Exit(code=1)
    state.console.print(f"Deleted {count} entries.")


@app.command("path")
def journal_path(ctx: typer.Context) -> None:
    """Print where the journal lives."""
    state = get_state(ctx)
    state.console.print(str(state.paths.data_dir / "journal.sqlite3"), highlight=False)
