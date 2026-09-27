"""Talking to OBS Studio through its built-in WebSocket server (obs-websocket 5, OBS 28+).

* Local only: SONU connects to 127.0.0.1 on the port OBS is set to, and reads the password from
  OBS's own settings (``~/.config/obs-studio/plugin_config/obs-websocket/config.json``). The
  password is never logged, shown or sent anywhere else.
* A deliberately small WebSocket client (RFC 6455: handshake, masked text frames, ping/pong,
  close) instead of another dependency — OBS's messages are small JSON documents.
* Protocol: Hello → Identify (with the SHA-256 challenge answer) → Identified, then Request /
  RequestResponse pairs. Events are not subscribed to; SONU reads state back after each change.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

HOST = "127.0.0.1"
DEFAULT_PORT = 4455
CONNECT_TIMEOUT_S = 3.0
REQUEST_TIMEOUT_S = 10.0
MAX_MESSAGE = 4 * 1024 * 1024
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _CLOSE, _PING, _PONG, _CONTINUATION = 0x1, 0x8, 0x9, 0xA, 0x0


class ObsError(Exception):
    """OBS isn't reachable, refused, or answered something unexpected."""


class ObsNotReachable(ObsError):
    """Nothing answers on OBS's port: OBS isn't running, or its WebSocket server is off."""


@dataclass(frozen=True, slots=True)
class ObsSettings:
    enabled: bool
    port: int
    password: str | None  # None: no authentication

    def __repr__(self) -> str:  # never print the password
        password = "set" if self.password else None
        return f"ObsSettings(enabled={self.enabled}, port={self.port}, password={password})"


def read_settings(home: Path) -> ObsSettings | None:
    """OBS's WebSocket settings, or None if OBS has none (never started, or too old)."""
    path = home / ".config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    port = data.get("server_port", DEFAULT_PORT)
    password = data.get("server_password") if data.get("auth_required", True) else None
    return ObsSettings(
        enabled=bool(data.get("server_enabled", False)),
        port=port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT,
        password=password if isinstance(password, str) and password else None,
    )


def auth_answer(password: str, salt: str, challenge: str) -> str:
    """obs-websocket 5: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


# ---------------------------------------------------------------------------- WebSocket framing
def encode_frame(opcode: int, payload: bytes, *, mask: bool) -> bytes:
    """One final frame. Clients must mask what they send; servers must not."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 1 << 16:
        header += bytes([mask_bit | 126]) + struct.pack("!H", length)
    else:
        header += bytes([mask_bit | 127]) + struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    return bytes(header) + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """(final, opcode, payload) — unmasking a masked frame (as a server receives them)."""
    first, second = await reader.readexactly(2)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))
    if length > MAX_MESSAGE:
        raise ObsError("OBS sent a message that is too large")
    key = await reader.readexactly(4) if second & 0x80 else None
    payload = await reader.readexactly(length)
    if key is not None:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bool(first & 0x80), first & 0x0F, payload


# ---------------------------------------------------------------------------- the client
class ObsClient:
    """``async with ObsClient(port, password) as obs: await obs.request("GetRecordStatus")``."""

    def __init__(self, port: int = DEFAULT_PORT, password: str | None = None, *, host: str = HOST) -> None:
        self.host, self.port, self._password = host, port, password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> Self:
        if self._writer is not None:  # already connected (entered once): the same connection
            return self
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
                await self._handshake()
                await self._identify()
        except TimeoutError:
            await self.close()
            raise ObsNotReachable(f"OBS didn't answer on port {self.port}") from None
        except OSError as exc:
            await self.close()
            raise ObsNotReachable(
                f"nothing is listening on port {self.port} ({exc.strerror or exc})"
            ) from None
        except (ObsError, asyncio.IncompleteReadError, ValueError) as exc:
            await self.close()
            raise (
                exc if isinstance(exc, ObsError) else ObsError(f"unexpected answer from OBS: {exc}")
            ) from None
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is None:
            return
        with contextlib.suppress(OSError, RuntimeError):
            writer.write(encode_frame(_CLOSE, struct.pack("!H", 1000), mask=True))
            await writer.drain()
        writer.close()
        with contextlib.suppress(OSError, RuntimeError):
            await writer.wait_closed()

    async def _handshake(self) -> None:
        reader, writer = self._streams()
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(
            (
                f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ObsError(f"OBS refused the connection: {lines[0]}")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)
        }
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode(), usedforsecurity=False).digest()
        ).decode()
        if headers.get("sec-websocket-accept") != accept:
            raise ObsError("OBS's handshake answer doesn't match")

    async def _identify(self) -> None:
        hello = await self._receive()
        if hello.get("op") != 0:
            raise ObsError("OBS didn't say hello")
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not self._password:
                raise ObsError("OBS wants a password, and none is set in OBS's WebSocket settings")
            identify["authentication"] = auth_answer(self._password, auth["salt"], auth["challenge"])
        await self._send({"op": 1, "d": identify})
        try:
            identified = await self._receive()
        except (ObsError, asyncio.IncompleteReadError):
            identified = {}
        if identified.get("op") != 2:
            raise ObsError("OBS didn't accept SONU's password (the one in OBS's WebSocket settings)")

    async def request(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """One OBS request; its responseData ({} if none). Raises ObsError if OBS says no."""
        request_id = secrets.token_hex(8)
        message: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if data:
            message["requestData"] = data
        await self._send({"op": 6, "d": message})
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            while True:
                reply = await self._receive()
                body = reply.get("d", {})
                if reply.get("op") == 7 and body.get("requestId") == request_id:
                    break
        status = body.get("requestStatus", {})
        if not status.get("result"):
            comment = status.get("comment") or f"code {status.get('code')}"
            raise ObsError(f"OBS refused {request_type}: {comment}")
        return body.get("responseData") or {}

    # -------------------------------------------------------------- frames
    def _streams(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise ObsError("not connected to OBS")
        return self._reader, self._writer

    async def _send(self, message: dict[str, Any]) -> None:
        _, writer = self._streams()
        writer.write(encode_frame(_TEXT, json.dumps(message).encode(), mask=True))
        await writer.drain()

    async def _receive(self) -> dict[str, Any]:
        reader, writer = self._streams()
        parts: list[bytes] = []
        while True:
            final, opcode, payload = await read_frame(reader)
            if opcode == _PING:
                writer.write(encode_frame(_PONG, payload, mask=True))
                await writer.drain()
                continue
            if opcode == _PONG:
                continue
            if opcode == _CLOSE:
                raise ObsError("OBS closed the connection")
            if opcode not in (_TEXT, _CONTINUATION):
                raise ObsError(f"unexpected WebSocket frame {opcode}")
            parts.append(payload)
            if sum(len(part) for part in parts) > MAX_MESSAGE:
                raise ObsError("OBS sent a message that is too large")
            if final:
                break
        message = json.loads(b"".join(parts))
        if not isinstance(message, dict):
            raise ObsError("OBS sent something that isn't a message")
        return message


@dataclass(frozen=True, slots=True)
class ObsLink:
    """Where SONU finds OBS: its own settings under ``home`` (default), or given ones (tests)."""

    home: Path | None = None
    settings: ObsSettings | None = None

    def current(self) -> ObsSettings | None:
        return self.settings or read_settings(self.home or Path.home())

    def client(self) -> ObsClient:
        settings = self.current()
        return ObsClient(settings.port if settings else DEFAULT_PORT, settings.password if settings else None)
