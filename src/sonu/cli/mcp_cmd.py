from __future__ import annotations

import asyncio
import shutil

import typer
from rich.markup import escape

from sonu.cli.state import AppState, env_snapshot, get_state
from sonu.mcp.server import McpServer, asks_first, offered_tools, serve_stdio
from sonu.runtime import SetupError, build_runtime
from sonu.tools.registry import ToolRegistry
from sonu.ui.desktop_ui import Picker

app = typer.Typer(
    help="SONU as an MCP server: Claude Code (or another MCP client) can use SONU's tools.",
    no_args_is_help=True,
)


@app.command("serve")
def mcp_serve(ctx: typer.Context) -> None:
    """Serve SONU's tools over stdio (an MCP client starts this; nothing to type here)."""
    state = get_state(ctx)
    state.require_valid_config()
    try:
        asyncio.run(_serve(state))
    except SetupError as exc:
        state.err.print(f"✖ {escape(str(exc))}", highlight=False)
        raise typer.Exit(code=1) from exc


async def _serve(state: AppState) -> None:
    state.paths.ensure_private_dirs()
    runtime = await build_runtime(state.settings, state.paths, env_snapshot())
    try:
        await serve_stdio(McpServer(runtime.engine, Picker.detect()))
    finally:
        await runtime.aclose()


@app.command("tools")
def mcp_tools(ctx: typer.Context) -> None:
    """List the tools a client gets, and which ones SONU asks you about first."""
    state = get_state(ctx)
    for name, tool in offered_tools(ToolRegistry()).items():
        mark = "asks first" if asks_first(tool) else tool.risk.value
        state.console.print(f"{name:28} {mark:11} {escape(tool.description)}", highlight=False)


@app.command("setup")
def mcp_setup(ctx: typer.Context) -> None:
    """Print the command that adds SONU to Claude Code."""
    state = get_state(ctx)
    sonu = shutil.which("sonu") or "sonu"
    state.console.print("Add SONU to Claude Code (for every folder), then restart Claude Code:\n")
    state.console.print(f"  claude mcp add --scope user sonu -- {sonu} mcp serve\n", highlight=False)
    state.console.print(
        "Claude Code then sees SONU's tools as mcp__sonu__…; SONU asks you on screen before anything that "
        "leaves the laptop, types, writes or removes. Window titles and other personal content stay here.",
        highlight=False,
    )
