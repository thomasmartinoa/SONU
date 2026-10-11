"""SONU as an MCP server (M5, D61): Claude Code — or another MCP client — can use SONU's tools.

* stdio transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (``sonu mcp serve``); logs go to stderr.
  No MCP SDK: the few methods below are all SONU needs, and every dependency is supply-chain surface.
* Each SONU tool is one MCP tool (``apps.launch`` → ``apps_launch``), its JSON schema the same one the
  planner gets. A call goes through ``Engine.call_tool``: arguments checked, resolved on a fresh
  snapshot, the policy, verified, audited — exactly like a planned step.
* Asking is SONU's own, on screen (rofi / fuzzel / wofi), never the client's: the client is a model.
  A step using words the user didn't say to SONU, or one that leaves the laptop, types or writes, is
  asked even when it's low risk by voice (``outside_caller_risk``). No picker → refused.
* Personal content (window titles, file names, the clipboard, the journal: ``private`` results) never
  goes back to the client — the client's model is a cloud model. SONU shows it to the user in a
  notification instead and the client is told so.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sonu import __version__
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import StepRecord
from sonu.policy.engine import ASK_OUTSIDE
from sonu.tools.base import Choices, Effect, Risk, StepStatus, Tool
from sonu.tools.registry import ToolRegistry
from sonu.ui.desktop_ui import Notifier, Picker

logger = logging.getLogger(__name__)

# Newest first; a client asking for one of these gets it, any other gets the newest.
PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Not offered: they need what only sonud or the user has (the microphone; the local prompt writer,
# whose whole prompt must be read before it's sent — a client can write its own).
NOT_OFFERED = frozenset({"voice.wake", "dev.prompt"})
MAX_QUESTION = 160  # what the picker shows in full (Picker.confirm cuts the rest)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

INSTRUCTIONS = (
    "SONU operates the user's Linux desktop (Arch + Hyprland) with typed tools: apps and windows, "
    "workspaces, volume, brightness, Wi-Fi and Bluetooth, media, the rice's theme and wallpaper, "
    "reminders, screenshots, OBS, Claude Code terminals, GitHub pull requests, installing software. "
    "A result says done only when SONU checked it on the desktop afterwards; 'unverified' means it "
    "couldn't tell. Some steps are asked on the user's screen first — if the user says no, don't "
    "retry. Personal content (window titles, file names, the clipboard, the activity journal) is "
    "never sent to you: SONU shows it to the user instead."
)


def mcp_name(tool_name: str) -> str:
    """MCP clients (and the Claude API) take letters, digits, _ and - in tool names."""
    return tool_name.replace(".", "_")


def offered_tools(registry: ToolRegistry) -> dict[str, Tool]:
    """The tools a client gets, by their MCP names."""
    return {mcp_name(tool.name): tool for tool in registry.tools if tool.name not in NOT_OFFERED}


def asks_first(tool: Tool) -> bool:
    """Whether SONU (usually) asks the user before this tool runs for a client."""
    return tool.risk.rank >= Risk.MEDIUM.rank or bool(tool.grounded) or bool(tool.effects & ASK_OUTSIDE)


def describe(name: str, tool: Tool, registry: ToolRegistry) -> dict[str, Any]:
    note = " SONU asks the user on screen first." if asks_first(tool) else ""
    return {
        "name": name,
        "description": f"{tool.description}{note}",
        "inputSchema": registry.args_schema(tool),
        "annotations": {
            "readOnlyHint": tool.risk is Risk.READ_ONLY,
            "destructiveHint": tool.risk.rank >= Risk.MEDIUM.rank,
            "openWorldHint": bool(tool.effects & {Effect.WEB_OPEN, Effect.GITHUB_WRITE}),
        },
    }


@dataclass
class McpServer:
    engine: Engine
    picker: Picker | None
    notifier: Notifier = field(default_factory=Notifier)
    client: str = "an MCP client"
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _refusal: str | None = None  # why the last confirmation was refused without asking

    def __post_init__(self) -> None:
        self.tools = offered_tools(self.engine.registry)

    # ---------------------------------------------------------------- JSON-RPC
    async def handle(self, message: Any) -> dict[str, Any] | None:
        """One incoming message → the response (None for a notification)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method, request_id = message.get("method"), message.get("id")
        if not isinstance(method, str):  # a response to us (SONU sends no requests) or junk
            return (
                None
                if "result" in message or "error" in message
                else _error(request_id, INVALID_REQUEST, "no method")
            )
        if "id" not in message:  # notifications: initialized, cancelled (handled by serve), …
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _result(request_id, self.initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": self.list_tools()})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, INVALID_PARAMS, "tools/call needs a name and an arguments object")
            if name not in self.tools:
                return _error(request_id, INVALID_PARAMS, f"unknown tool: {name}")
            return _result(request_id, await self.call(name, arguments))
        return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        info = params.get("clientInfo")
        if isinstance(info, dict) and isinstance(info.get("name"), str) and info["name"].strip():
            self.client = " ".join(info["name"].split())[:40]
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "sonu", "title": "SONU", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    # ---------------------------------------------------------------- tools
    def list_tools(self) -> list[dict[str, Any]]:
        return [describe(name, tool, self.engine.registry) for name, tool in self.tools.items()]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools[name]
        async with self._lock:  # one at a time: the engine's context belongs to one step
            self._refusal = None
            record = await self.engine.call_tool(
                tool.name, arguments, caller=f"mcp:{self.client}", confirm=self._confirm, choose=self._choose
            )
            text = await self._report(record)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": record.outcome.status not in (StepStatus.DONE, StepStatus.UNVERIFIED),
        }

    async def _report(self, record: StepRecord) -> str:
        outcome = record.outcome
        if outcome.private:
            shown = await self.notifier.send(f"SONU, for {self.client}", outcome.summary)
            where = "showed it to the user in a notification" if shown else "kept it on the laptop"
            text = (
                f"{outcome.status.value}: {record.description}. The answer has personal content "
                f"(window titles, names), so it isn't sent to you — SONU {where}."
            )
        else:
            text = f"{outcome.status.value}: {outcome.summary}"
            if outcome.question:
                text += f"\n{outcome.question}"
        if self._refusal:
            text += f"\n{self._refusal}"
        if outcome.status is StepStatus.DONE and outcome.checks:
            text += f"\n(checked on the desktop afterwards: {len(outcome.checks)} passed)"
        return text

    # ---------------------------------------------------------------- the user, on screen
    async def _confirm(self, request: ConfirmRequest) -> bool:
        if self.picker is None:
            self._refusal = (
                "No on-screen picker (rofi, fuzzel or wofi) to ask the user, so SONU didn't do it."
            )
            return False
        question = f"{self.client} wants SONU to: {request.description} ({request.risk.value} risk). Allow?"
        if request.details or len(question) > MAX_QUESTION:
            self._refusal = "Too long to show the user in full to approve, so SONU didn't do it."
            return False
        approved = await self.picker.confirm(question)
        if not approved:
            self._refusal = "The user said no on screen (or didn't answer) — don't ask again."
        return approved

    async def _choose(self, choices: Choices) -> str | None:
        if self.picker is None:
            return None
        index = await self.picker.choose(choices.prompt, [option.label for option in choices.options])
        return None if index is None else choices.options[index].value


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# -------------------------------------------------------------------- stdio
async def serve(
    server: McpServer,
    read_line: Callable[[], Awaitable[bytes]],
    write: Callable[[bytes], None],
) -> None:
    """Read messages until the client closes stdin; requests run concurrently (a ping is answered
    while a step waits for the user), tool calls one at a time; ``notifications/cancelled`` cancels."""
    running: dict[Any, asyncio.Task[None]] = {}

    def send(message: dict[str, Any]) -> None:
        write(json.dumps(message, ensure_ascii=False).encode() + b"\n")

    async def answer(message: Any) -> None:
        try:
            response = await server.handle(message)
        except Exception:  # a bug must not kill the server or leave the client waiting
            logger.exception("MCP request failed")
            request_id = message.get("id") if isinstance(message, dict) else None
            response = _error(request_id, -32603, "internal error in SONU")
        if response is not None:
            send(response)

    while line := await read_line():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            send(_error(None, PARSE_ERROR, "not valid JSON"))
            continue
        if isinstance(message, dict) and message.get("method") == "notifications/cancelled":
            params = message.get("params") or {}
            task = running.get(params.get("requestId")) if isinstance(params, dict) else None
            if task is not None:
                task.cancel()  # no response for a cancelled request
            continue
        request_id = message.get("id") if isinstance(message, dict) else None
        task = asyncio.create_task(answer(message))
        if request_id is not None:
            running[request_id] = task
            task.add_done_callback(functools.partial(_finished, running, request_id))
    for task in list(running.values()):
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _finished(running: dict[Any, asyncio.Task[None]], request_id: Any, task: asyncio.Task[None]) -> None:
    if running.get(request_id) is task:
        del running[request_id]


async def serve_stdio(server: McpServer) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer

    def write(data: bytes) -> None:
        out.write(data)
        out.flush()

    await serve(server, reader.readline, write)
