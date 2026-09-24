"""Read-only views of the desktop as SONU sees it — handy for debugging and bug reports."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import typer
from rich.markup import escape
from rich.table import Table

from sonu.cli.state import AppState, env_snapshot, get_state
from sonu.desktop.hyprland import (
    HyprlandClient,
    HyprlandError,
    discover_instance,
    stream_events,
)

app = typer.Typer(help="Look at the desktop the way SONU sees it (read-only).", no_args_is_help=True)

# Events whose last field is a window title (hidden with --no-titles).
_TITLE_EVENTS = {"openwindow", "windowtitlev2", "activewindow"}


def _client(state: AppState) -> HyprlandClient:
    try:
        instance = discover_instance(env_snapshot())
    except HyprlandError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    return HyprlandClient(instance, timeout_s=state.settings.hyprland.request_timeout_s)


@app.command("windows")
def windows(
    ctx: typer.Context,
    no_titles: bool = typer.Option(False, "--no-titles", help="Hide window titles (for sharing output)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List open windows: workspace, class, and (optionally) title."""
    state = get_state(ctx)
    state.require_valid_config()
    client = _client(state)
    try:
        result = asyncio.run(client.clients())
    except HyprlandError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    result.sort(key=lambda window: (window.workspace.id, window.window_class, window.address))
    if as_json:
        rows = [
            {
                "workspace": window.workspace.name,
                "class": window.window_class,
                "initialClass": window.initial_class,
                "title": None if no_titles else window.title,
                "floating": window.floating,
                "stableId": window.stable_id,
            }
            for window in result
        ]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    table = Table()
    table.add_column("ws", justify="right")
    table.add_column("class")
    table.add_column("initial class")
    if not no_titles:
        table.add_column("title", overflow="fold")
    table.add_column("floating")
    for window in result:
        row = [window.workspace.name, escape(window.window_class), escape(window.initial_class)]
        if not no_titles:
            row.append(escape(window.title))
        row.append("yes" if window.floating else "")
        table.add_row(*row)
    state.console.print(table)


@app.command("workspaces")
def workspaces(ctx: typer.Context) -> None:
    """List workspaces and how many windows each holds."""
    state = get_state(ctx)
    state.require_valid_config()
    client = _client(state)
    try:
        result = asyncio.run(client.workspaces())
    except HyprlandError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    table = Table()
    table.add_column("id", justify="right")
    table.add_column("name")
    table.add_column("monitor")
    table.add_column("windows", justify="right")
    for workspace in sorted(result, key=lambda item: item.id):
        table.add_row(
            str(workspace.id), escape(workspace.name), escape(workspace.monitor), str(workspace.windows)
        )
    state.console.print(table)


@app.command("monitors")
def monitors(ctx: typer.Context) -> None:
    """List monitors."""
    state = get_state(ctx)
    state.require_valid_config()
    client = _client(state)
    try:
        result = asyncio.run(client.monitors())
    except HyprlandError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    table = Table()
    table.add_column("name")
    table.add_column("mode")
    table.add_column("scale", justify="right")
    table.add_column("workspace")
    table.add_column("focused")
    for monitor in result:
        table.add_row(
            escape(monitor.name),
            f"{monitor.width}x{monitor.height}@{monitor.refresh_rate:.0f}",
            f"{monitor.scale:g}",
            escape(monitor.active_workspace.name),
            "yes" if monitor.focused else "",
        )
    state.console.print(table)


@app.command("events")
def events(
    ctx: typer.Context,
    count: int = typer.Option(0, "--count", min=0, help="Stop after this many events (0 = until Ctrl+C)."),
    no_titles: bool = typer.Option(False, "--no-titles", help="Hide window titles."),
) -> None:
    """Stream live Hyprland events (what SONU will react to)."""
    state = get_state(ctx)
    state.require_valid_config()
    try:
        instance = discover_instance(env_snapshot())
    except HyprlandError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    async def run() -> None:
        seen = 0
        async for event in stream_events(instance):
            fields = list(event.fields)
            if no_titles and event.name in _TITLE_EVENTS and fields:
                fields[-1] = "«title hidden»"
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            state.console.print(
                f"[dim]{stamp}[/] [bold]{escape(event.name)}[/] {escape(' | '.join(fields))}", highlight=False
            )
            seen += 1
            if count and seen >= count:
                return

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except HyprlandError as exc:
        state.err.print(f"[red]error:[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
