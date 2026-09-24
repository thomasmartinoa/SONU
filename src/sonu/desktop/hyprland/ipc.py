"""Client for Hyprland's request socket (the protocol ``hyprctl`` speaks).

Hyprland handles this socket **synchronously on the compositor's main thread**
(``hyprCtlFDTick`` in ``src/debug/HyprCtl.cpp``). A misbehaving client can stall the whole
desktop for up to five seconds, so this client is deliberately careful:

* one short-lived connection per request, always closed;
* the whole request goes out in a single write, then the write side is shut down, so the
  compositor's read loop (1023-byte chunks, stops on a short read) sees EOF and never blocks;
* requests are capped well below 1023 bytes and must be a single line;
* every step has a client-side timeout.

Requests use Hyprland's flag syntax, e.g. ``j/clients`` for JSON output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from sonu.desktop.hyprland.errors import HyprlandProtocolError, HyprlandRequestError
from sonu.desktop.hyprland.instance import HyprlandInstance
from sonu.desktop.hyprland.models import (
    ConfigProvider,
    HyprlandStatus,
    HyprlandVersion,
    Monitor,
    Window,
    Workspace,
)

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1000
MAX_REPLY_BYTES = 16 * 1024 * 1024
UNKNOWN_REQUEST = "unknown request"

_M = TypeVar("_M", bound=BaseModel)


def _validate_request(command: str) -> bytes:
    if not command or command != command.strip():
        raise HyprlandRequestError("request must be non-empty and have no surrounding whitespace")
    if any(char in command for char in "\n\r\x00"):
        raise HyprlandRequestError("request must be a single line without NUL bytes")
    encoded = command.encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise HyprlandRequestError(f"request too long ({len(encoded)} > {MAX_REQUEST_BYTES} bytes)")
    return encoded


class HyprlandClient:
    def __init__(self, instance: HyprlandInstance, *, timeout_s: float = 2.0) -> None:
        self.instance = instance
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ transport
    async def request(self, command: str) -> str:
        """Send one raw request and return Hyprland's reply text."""
        payload = _validate_request(command)
        socket_path = self.instance.request_socket
        try:
            async with asyncio.timeout(self.timeout_s):
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                try:
                    writer.write(payload)
                    await writer.drain()
                    if writer.can_write_eof():
                        writer.write_eof()
                    chunks: list[bytes] = []
                    total = 0
                    while chunk := await reader.read(64 * 1024):
                        total += len(chunk)
                        if total > MAX_REPLY_BYTES:
                            raise HyprlandProtocolError(
                                f"reply to {command!r} exceeds {MAX_REPLY_BYTES} bytes"
                            )
                        chunks.append(chunk)
                finally:
                    writer.close()
                    with contextlib.suppress(ConnectionError, OSError):  # peer already gone
                        await writer.wait_closed()
        except TimeoutError as exc:
            raise HyprlandRequestError(
                f"Hyprland did not answer {command!r} within {self.timeout_s}s"
            ) from exc
        except OSError as exc:
            raise HyprlandRequestError(f"cannot talk to Hyprland at {socket_path}: {exc}") from exc

        text = b"".join(chunks).decode("utf-8", errors="replace")
        logger.debug("hyprland %s -> %d bytes", command, len(text))
        return text

    async def request_json(self, command: str) -> Any:
        """Send ``j/<command>`` and decode the JSON reply."""
        text = await self.request(f"j/{command}")
        if text.strip() == UNKNOWN_REQUEST:
            raise HyprlandProtocolError(f"Hyprland does not support {command!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:120].replace("\n", " ")
            raise HyprlandProtocolError(f"invalid JSON in reply to {command!r}: {snippet!r}") from exc

    async def _typed(self, command: str, model: type[_M]) -> _M:
        data = await self.request_json(command)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise HyprlandProtocolError(
                f"unexpected shape for {command!r}: {exc.errors()[0]['msg']}"
            ) from exc

    async def _typed_list(self, command: str, adapter: TypeAdapter[list[_M]]) -> list[_M]:
        data = await self.request_json(command)
        try:
            return adapter.validate_python(data)
        except ValidationError as exc:
            raise HyprlandProtocolError(
                f"unexpected shape for {command!r}: {exc.errors()[0]['msg']}"
            ) from exc

    # ------------------------------------------------------------------ queries
    async def version(self) -> HyprlandVersion:
        return await self._typed("version", HyprlandVersion)

    async def status(self) -> HyprlandStatus | None:
        """``j/status`` exists since the Lua migration (0.55); older versions return None."""
        text = await self.request("j/status")
        if text.strip() == UNKNOWN_REQUEST:
            return None
        try:
            return HyprlandStatus.model_validate_json(text)
        except ValidationError as exc:
            raise HyprlandProtocolError(f"unexpected shape for 'status': {exc.errors()[0]['msg']}") from exc

    async def config_provider(self) -> ConfigProvider:
        """Which config system is active — this decides the dispatcher syntax SONU must use.

        With a Lua config, ``dispatch <x>`` is evaluated as ``hl.dispatch(<x>)``; with hyprlang it
        is the classic ``dispatch <name> <args>`` form (verified against Hyprland 0.56.2).
        """
        status = await self.status()
        if status is None:
            return "hyprlang"
        if status.config_provider in ("lua", "hyprlang"):
            return "lua" if status.config_provider == "lua" else "hyprlang"
        raise HyprlandProtocolError(f"unknown config provider {status.config_provider!r}")

    async def clients(self) -> list[Window]:
        return await self._typed_list("clients", _WINDOWS)

    async def workspaces(self) -> list[Workspace]:
        return await self._typed_list("workspaces", _WORKSPACES)

    async def monitors(self) -> list[Monitor]:
        return await self._typed_list("monitors", _MONITORS)

    async def active_window(self) -> Window | None:
        data = await self.request_json("activewindow")
        if data == {}:
            return None
        try:
            return Window.model_validate(data)
        except ValidationError as exc:
            raise HyprlandProtocolError(
                f"unexpected shape for 'activewindow': {exc.errors()[0]['msg']}"
            ) from exc


_WINDOWS = TypeAdapter(list[Window])
_WORKSPACES = TypeAdapter(list[Workspace])
_MONITORS = TypeAdapter(list[Monitor])
