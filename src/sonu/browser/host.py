"""SONU's native messaging host (D70): the browser starts it for SONU's extension; it relays SONU's
requests to the extension and the answers back.

Standard library only (the browser starts it for every window session). It opens a socket in SONU's
0700 runtime folder (``$XDG_RUNTIME_DIR/sonu/browsers/<browser>-<pid>.sock``, 0600); SONU connects,
sends one request as a JSON line ({"op": "tabs"}), and reads one JSON line back. It stops when the
browser closes its stdin, removing the socket.

The browser's side is its native messaging framing: a 4-byte length (native byte order) and JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import struct
import sys
from pathlib import Path
from typing import Any

MAX_MESSAGE = 8 * 1024 * 1024  # what browsers allow a host to receive is 4 GB; SONU never needs this much
REQUEST_TIMEOUT_S = 5.0
_BROWSERS = {"zen-bin": "zen", "zen": "zen", "firefox": "firefox", "firefox-bin": "firefox"}


def socket_folder(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    base = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "sonu" / "browsers"


def browser_of(pid: int, proc: Path = Path("/proc")) -> str:
    """Which browser started the host (its parent's program): "zen", "chromium", "firefox"…"""
    try:
        name = (proc / str(pid) / "exe").readlink().name
    except OSError:
        return "browser"
    if name in _BROWSERS:
        return _BROWSERS[name]
    return "chromium" if "chrom" in name else name or "browser"


class Host:
    def __init__(self, writer: Any, browser: str) -> None:
        self.out = writer  # binary stdout
        self.browser = browser
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.ids = itertools.count(1)
        self.hello: dict[str, Any] = {}

    # ------------------------------------------------------------------ the browser's side
    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message).encode()
        self.out.write(struct.pack("=I", len(data)) + data)
        self.out.flush()

    def received(self, message: dict[str, Any]) -> None:
        if "hello" in message and isinstance(message["hello"], dict):
            self.hello = message["hello"]
            return
        future = self.pending.get(message.get("id"))  # type: ignore[arg-type]
        if future is not None and not future.done():
            future.set_result(message)

    async def read_browser(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                header = await reader.readexactly(4)
                (length,) = struct.unpack("=I", header)
                if length > MAX_MESSAGE:
                    return
                message = json.loads(await reader.readexactly(length))
            except (asyncio.IncompleteReadError, ValueError):
                return  # the browser closed the pipe (it's quitting) or sent something broken
            if isinstance(message, dict):
                self.received(message)

    # ------------------------------------------------------------------ SONU's side
    async def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = next(self.ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.send({**request, "id": request_id})
            return await asyncio.wait_for(future, REQUEST_TIMEOUT_S)
        except TimeoutError:
            return {"ok": False, "error": "the browser didn't answer"}
        finally:
            self.pending.pop(request_id, None)

    async def serve_sonu(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_S)
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise ValueError("not a request")
            if request["op"] == "hello":
                reply: dict[str, Any] = {"ok": True, "result": {**self.hello, "browser": self.browser}}
            else:
                reply = await self.ask(request)
            reply.pop("id", None)
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except (TimeoutError, ValueError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def run(env: dict[str, str] | None = None) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE + 4)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    host = Host(sys.stdout.buffer, browser_of(os.getppid()))
    folder = socket_folder(env)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / f"{host.browser}-{os.getpid()}.sock"
    server = await asyncio.start_unix_server(host.serve_sonu, path=str(path))
    path.chmod(0o600)
    try:
        await host.read_browser(reader)
    finally:
        server.close()
        path.unlink(missing_ok=True)


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
