"""Talking to an outside MCP server over stdio (D63): SONU is the client.

* The server is started from the user's argv (never a shell), in its own session, with a small
  environment: PATH, HOME, the locale and XDG folders, and the ``env`` the user gave it — not the
  Wayland, Hyprland or D-Bus sockets (an outside program must not drive the desktop behind SONU's
  back) and no other secrets.
* Newline-delimited JSON-RPC 2.0. SONU offers the server nothing (no sampling, roots or elicitation):
  any request from it is answered "method not found".
* Every request has a timeout; a timed-out call is cancelled (``notifications/cancelled``).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonu import __version__

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT_S = 45.0  # `uvx` / `npx` may download the server the first time
LIST_TIMEOUT_S = 15.0
MAX_TOOLS = 32
MAX_LINE = 8 * 1024 * 1024
PASSED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)


class McpError(Exception):
    """The server couldn't be started, stopped, answered with an error or didn't answer in time."""


class McpTimeout(McpError):
    """No answer in time (the server may still be busy with it)."""


@dataclass(frozen=True, slots=True)
class RemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only_hint: bool | None  # what the server says — shown to the user, never trusted


@dataclass(frozen=True, slots=True)
class CallResult:
    text: str
    is_error: bool


def server_env(extra: Mapping[str, str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    base = os.environ if base is None else base
    return {**{key: base[key] for key in PASSED_ENV if key in base}, **extra}


def expand_argv(command: Sequence[str]) -> list[str]:
    """``~/bin/server`` → the home folder's (no other expansion: no shell)."""
    return [str(Path(part).expanduser()) if part[:2] in ("~", "~/") else part for part in command]


class McpConnection:
    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        label: str = "MCP server",
    ) -> None:
        self.argv = expand_argv(command)
        self.env = server_env(env or {})
        self.label = label
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._tasks: list[asyncio.Task[None]] = []
        self._stderr: deque[str] = deque(maxlen=4)
        self._reading = False
        self.server_name = ""

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None and self._reading

    # ---------------------------------------------------------------- lifetime
    async def start(self, timeout_s: float = START_TIMEOUT_S) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                start_new_session=True,
                limit=MAX_LINE,
            )
        except OSError as exc:
            raise McpError(f"couldn't start {self.label} ({self.argv[0]}): {exc.strerror or exc}") from exc
        stdout, stderr = self._process.stdout, self._process.stderr
        if stdout is None or stderr is None:  # never: both are pipes
            raise McpError(f"couldn't talk to {self.label}")
        self._reading = True
        self._tasks = [
            asyncio.create_task(self._read_messages(stdout)),
            asyncio.create_task(self._read_stderr(stderr)),
        ]
        try:
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "sonu", "title": "SONU", "version": __version__},
                },
                timeout_s=timeout_s,
            )
        except McpError:
            await self.close()
            raise
        info = result.get("serverInfo")
        self.server_name = str(info.get("name", "")) if isinstance(info, dict) else ""
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def close(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self._fail_pending("it was stopped")

    # ---------------------------------------------------------------- MCP
    async def list_tools(self) -> list[RemoteTool]:
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _ in range(10):  # pages
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params, timeout_s=LIST_TIMEOUT_S)
            for raw in result.get("tools") or []:
                if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                    annotations = raw.get("annotations")
                    hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
                    schema = raw.get("inputSchema")
                    tools.append(
                        RemoteTool(
                            raw["name"],
                            str(raw.get("description") or ""),
                            schema if isinstance(schema, dict) else {},
                            hint if isinstance(hint, bool) else None,
                        )
                    )
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> CallResult:
        result = await self.request("tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s)
        return CallResult(result_text(result), bool(result.get("isError")))

    # ---------------------------------------------------------------- JSON-RPC
    async def request(self, method: str, params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        if not self.alive:
            raise McpError(f"{self.label} isn't running{self._last_words()}")
        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            message = await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            with contextlib.suppress(McpError):
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": request_id, "reason": "timed out"},
                    }
                )
            raise McpTimeout(f"{self.label} didn't answer in {timeout_s:g} s") from None
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, dict) else error
            raise McpError(f"{self.label}: {str(detail)[:300]}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise McpError(f"{self.label} isn't running{self._last_words()}")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await process.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise McpError(f"{self.label} stopped{self._last_words()}") from exc

    async def _read_messages(self, stdout: asyncio.StreamReader) -> None:
        try:
            while line := await stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("%s wrote a line that isn't JSON", self.label)
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        except (ValueError, OSError) as exc:  # a line over the limit, or the pipe broke
            logger.warning("%s: %s", self.label, exc)
        finally:
            self._reading = False
            self._fail_pending(f"it stopped{self._last_words()}")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:  # a request to SONU: it offers nothing
                with contextlib.suppress(McpError):
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32601, "message": "SONU offers no client features"},
                        }
                    )
            return
        future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if future is not None and not future.done():
            future.set_result(message)

    async def _read_stderr(self, stderr: asyncio.StreamReader) -> None:
        with contextlib.suppress(ValueError, OSError):
            while line := await stderr.readline():
                text = line.decode(errors="replace").strip()
                if text:
                    self._stderr.append(text[:200])

    def _fail_pending(self, why: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(f"{self.label}: {why}"))

    def _last_words(self) -> str:
        return f" ({self._stderr[-1]})" if self._stderr else ""


def result_text(result: dict[str, Any]) -> str:
    """A tools/call result as text: its text parts; other parts named; structured content as JSON."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif kind == "resource":
            resource = item.get("resource")
            text = resource.get("text") if isinstance(resource, dict) else None
            parts.append(text if isinstance(text, str) else "[a file]")
        elif kind == "resource_link":
            parts.append(f"[{item.get('name') or item.get('uri') or 'a link'}]")
        elif kind in ("image", "audio"):
            parts.append(f"[{'a picture' if kind == 'image' else 'a sound'}]")
    if not parts and isinstance(result.get("structuredContent"), dict | list):
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)
