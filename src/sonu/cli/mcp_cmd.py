from __future__ import annotations

import asyncio
import shutil

import typer
from rich.markup import escape

from sonu.cli.state import AppState, env_snapshot, get_state
from sonu.mcp.outside import OutsideTool, plain, start_servers
from sonu.mcp.server import McpServer, asks_first, offered_tools, serve_stdio
from sonu.runtime import SetupError, build_runtime
from sonu.tools.base import Risk
from sonu.tools.registry import ToolRegistry
from sonu.ui.desktop_ui import Picker

app = typer.Typer(
    help="MCP: Claude Code can use SONU's tools (serve), and SONU can use other servers' tools (check).",
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
    runtime = await build_runtime(state.settings, state.paths, env_snapshot(), outside=False)
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


@app.command("check")
def mcp_check(ctx: typer.Context) -> None:
    """Start the MCP servers you added ([mcp.servers]) and list their tools, and how SONU treats each."""
    state = get_state(ctx)
    state.require_valid_config()
    if not state.settings.mcp.servers:
        state.console.print("No MCP servers added. Add one in your config:\n")
        state.console.print(MCP_EXAMPLE, highlight=False, markup=False)
        return
    asyncio.run(_check(state))


MCP_EXAMPLE = """\
  [mcp.servers.fetch]
  command = ["uvx", "mcp-server-fetch"]
  reaches = "web"          # local | web | account — what it can reach
  read_only = ["fetch"]    # tools that only read: run without asking when the words are yours
"""

TREATMENT = {
    "local": "runs without asking",
    "web": "runs without asking when every word it sends is yours",
    "account": "runs without asking when every word it sends is yours",
}


def treatment(tool: OutsideTool) -> str:
    """How SONU treats an outside tool, and what the server says of it (shown, never trusted)."""
    if tool.risk is Risk.READ_ONLY:
        said = TREATMENT[tool.server.settings.reaches]
    else:
        said = "asks every time (high risk)"
    hint = tool.remote.read_only_hint
    if hint is not None:
        said += f" · the server says {'it only reads' if hint else 'it changes things'}"
    return said


async def _check(state: AppState) -> None:
    outside = await start_servers(state.settings.mcp)
    try:
        for name, server in state.settings.mcp.servers.items():
            if not server.enabled:
                state.console.print(f"[bold]{name}[/] — off (enabled = false)")
                continue
            if name in outside.problems and not any(t.server.name == name for t in outside.tools):
                state.console.print(f"[bold]{name}[/] ✖ {escape(outside.problems[name])}")
                continue
            tools = [tool for tool in outside.tools if tool.server.name == name]
            count = f"{len(tools)} tool{'' if len(tools) == 1 else 's'}"
            state.console.print(f"[bold]{name}[/] ✔ {count} · reaches: {server.reaches}")
            if name in outside.problems:
                state.console.print(f"  ⚠ {escape(outside.problems[name])}")
            for tool in tools:
                state.console.print(f"  {escape(tool.name)} — {treatment(tool)}", highlight=False)
                state.console.print(f"    {escape(plain(tool.remote.description, 100))}", highlight=False)
    finally:
        await outside.aclose()


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
