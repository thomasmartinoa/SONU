from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from sonu.audit import AuditError, AuditLog
from sonu.cli.state import get_state

app = typer.Typer(help="Inspect SONU's append-only audit log.", no_args_is_help=True)


@app.command("path")
def audit_path(ctx: typer.Context) -> None:
    """Print where the audit log lives."""
    state = get_state(ctx)
    state.console.print(str(state.paths.audit_file), highlight=False)


@app.command("tail")
def audit_tail(
    ctx: typer.Context,
    count: int = typer.Option(20, "-n", "--count", min=1, max=10_000, help="How many records to show."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON Lines instead of a table."),
) -> None:
    """Show the most recent audit records."""
    state = get_state(ctx)
    state.require_valid_config()
    try:
        records = AuditLog(state.paths.audit_file).tail(count)
    except AuditError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if as_json:
        for record in records:
            print(record.model_dump_json())
        return
    if not records:
        state.console.print("the audit log is empty")
        return
    table = Table(show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("time (UTC)", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("data", overflow="fold")
    for record in records:
        table.add_row(
            str(record.seq), record.ts, record.kind, escape(json.dumps(record.data, ensure_ascii=False))
        )
    state.console.print(table)


@app.command("verify")
def audit_verify(ctx: typer.Context) -> None:
    """Check the hash chain of the whole log."""
    state = get_state(ctx)
    state.require_valid_config()
    result = AuditLog(state.paths.audit_file).verify()
    if result.ok:
        state.console.print(f"[green]✔[/] {result.records} record(s), hash chain intact")
        return
    state.err.print(f"[red]✖[/] line {result.line}: {escape(result.error or 'unknown problem')}")
    raise typer.Exit(code=1)
