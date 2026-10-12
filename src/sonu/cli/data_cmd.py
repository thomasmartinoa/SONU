"""``sonu memory`` and ``sonu conversations``: see and delete what SONU keeps about you (D90, part 3) — what
the Control Center's "Your data" pages run. Deleting asks first (``--yes`` once you're sure: the window asks
itself) and is written to the audit log — how many, never what."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from typing import Any, Literal

import typer
from rich.markup import escape

from sonu.audit.log import AuditError, AuditLog
from sonu.cli.state import AppState, get_state
from sonu.core.talks import Talks
from sonu.runtime import load_memory, open_talks
from sonu.tools.journal_tools import period

memory_app = typer.Typer(help='What you asked SONU to remember ("remember …").', no_args_is_help=True)
talks_app = typer.Typer(
    help="What you and SONU said to each other (kept on this laptop).", no_args_is_help=True
)

Since = Literal["last_hour", "today", "everything"]


def _sure(state: AppState, question: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty() or not typer.confirm(question, default=False):
        state.err.print("Nothing deleted (add --yes once you're sure).")
        raise typer.Exit(1)


def _audit(state: AppState, what: str, count: int) -> None:
    if not state.settings.audit.enabled:
        return
    try:
        AuditLog(state.paths.audit_file).append("data.forget", {"what": what, "count": count})
    except (OSError, AuditError) as exc:
        state.err.print(f"[yellow]▲[/] couldn't write the audit log: {exc}")


# ---------------------------------------------------------------------------- sonu memory
@memory_app.command("list")
def memory_list(
    ctx: typer.Context, as_json: bool = typer.Option(False, "--json", help="Print JSON.")
) -> None:
    """Everything SONU remembers about you, numbered."""
    state = get_state(ctx)
    state.require_valid_config()
    items = load_memory(state.paths).items
    rows = [
        {"n": n, "kind": item.kind, "text": item.describe(), "at": item.at} for n, item in enumerate(items, 1)
    ]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        state.console.print("SONU doesn't remember anything about you yet.")
    for row in rows:
        state.console.print(
            f"  {row['n']}. {escape(str(row['text']))}  [dim]{row['kind']}[/]", highlight=False
        )


@memory_app.command("forget")
def memory_forget(
    ctx: typer.Context,
    number: int = typer.Argument(None, help="The item's number in `sonu memory list`."),
    everything: bool = typer.Option(False, "--all", help="Forget everything."),
    yes: bool = typer.Option(False, "--yes", help="Already confirmed."),
) -> None:
    """Forget one thing (by its number), or everything."""
    state = get_state(ctx)
    state.require_valid_config()
    if (number is None) == (not everything):
        state.err.print("[red]error:[/] give a number or --all")
        raise typer.Exit(2)
    memory = load_memory(state.paths)
    if everything:
        gone = list(memory.items)
    elif 1 <= number <= len(memory.items):
        gone = [memory.items[number - 1]]
    else:
        state.err.print(f"[red]error:[/] there's no item {number} (see `sonu memory list`)")
        raise typer.Exit(1)
    if not gone:
        state.console.print("Nothing to forget.")
        return
    what = "everything SONU remembers" if everything else f"“{gone[0].describe()}”"
    _sure(state, f"Forget {what}?", yes)
    memory.remove(gone)
    left = [item for item in load_memory(state.paths).items if item in gone]
    if left:
        state.err.print("[red]✖[/] it's still there")
        raise typer.Exit(1)
    _audit(state, "memory", len(gone))
    state.console.print(f"[green]✔[/] Forgot {len(gone)} thing{'s' if len(gone) != 1 else ''}.")


# ---------------------------------------------------------------------------- sonu conversations
def _talks(state: AppState) -> Talks:
    state.require_valid_config()
    talks = open_talks(state.settings, state.paths)
    if talks is None:
        state.err.print("SONU doesn't keep conversations ([conversations] keep = false).")
        raise typer.Exit(1)
    return talks


@talks_app.command("list")
def talks_list(
    ctx: typer.Context,
    when: str = typer.Argument("today", help="today, yesterday, this_week or all"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """What you and SONU said: today (by default), yesterday, this week or all."""
    state = get_state(ctx)
    talks = _talks(state)
    now = datetime.now()
    if when == "all":
        start, end = now - timedelta(days=state.settings.conversations.keep_days), now
    elif when in ("today", "yesterday", "this_week"):
        start, end, _ = period(when, now)  # type: ignore[arg-type]  # checked
    else:
        state.err.print("[red]error:[/] when: today, yesterday, this_week or all")
        raise typer.Exit(2)
    found = talks.between(start.timestamp(), end.timestamp())
    talks.close()
    rows: list[dict[str, Any]] = [
        {
            "at": e.at,
            "time": datetime.fromtimestamp(e.at).strftime("%a %H:%M"),
            "request": e.request,
            "reply": e.reply,
        }
        for e in found
    ]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        state.console.print("Nothing said then.")
    for row in rows:
        state.console.print(
            f"[dim]{row['time']}[/]  “{escape(row['request'])}”\n        {escape(row['reply'])}"
        )


@talks_app.command("forget")
def talks_forget(
    ctx: typer.Context,
    since: str = typer.Option(..., "--since", help="last_hour, today or everything"),
    yes: bool = typer.Option(False, "--yes", help="Already confirmed."),
) -> None:
    """Delete our conversations from the last hour, today, or all of them."""
    state = get_state(ctx)
    now = datetime.now()
    starts = {
        "last_hour": (now - timedelta(hours=1)).timestamp(),
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
        "everything": 0.0,
    }
    if since not in starts:
        state.err.print("[red]error:[/] --since: last_hour, today or everything")
        raise typer.Exit(2)
    talks = _talks(state)
    count = talks.count(starts[since])
    if not count:
        talks.close()
        state.console.print("Nothing to delete.")
        return
    _sure(state, f"Delete {count} request{'s' if count != 1 else ''} and SONU's answers?", yes)
    talks.forget(starts[since])
    left = talks.count(starts[since])
    talks.close()
    if left:
        state.err.print(f"[red]✖[/] {left} are still there")
        raise typer.Exit(1)
    _audit(state, "conversations", count)
    state.console.print(f"[green]✔[/] Deleted {count} request{'s' if count != 1 else ''}.")
