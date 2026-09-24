"""A fake Hyprland compositor for tests, demos and (later) the eval suite.

It serves *real* Unix sockets laid out exactly like Hyprland's
(``<runtime>/hypr/<signature>/.socket.sock`` and ``.socket2.sock``), reads requests the way
Hyprland 0.56.2 does (1023-byte chunks, stop on a short read), answers with JSON shaped like
the real thing, and can broadcast events. SONU code under test cannot tell the difference.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from sonu.desktop.hyprland.instance import EVENT_SOCKET_NAME, REQUEST_SOCKET_NAME, HyprlandInstance

_READ_CHUNK = 1023


def make_window(
    address: str,
    window_class: str,
    title: str = "",
    *,
    workspace_id: int = 1,
    workspace_name: str | None = None,
    monitor: int = 0,
    pid: int = 1000,
    stable_id: str | None = None,
    floating: bool = False,
    focus_history_id: int = 0,
) -> dict[str, Any]:
    """A window as ``j/clients`` reports it on Hyprland 0.56.2."""
    return {
        "address": address,
        "mapped": True,
        "hidden": False,
        "visible": True,
        "acceptsInput": True,
        "at": [0, 0],
        "size": [1280, 720],
        "workspace": {"id": workspace_id, "name": workspace_name or str(workspace_id)},
        "floating": floating,
        "monitor": monitor,
        "class": window_class,
        "title": title,
        "initialClass": window_class,
        "initialTitle": title,
        "pid": pid,
        "xwayland": False,
        "pinned": False,
        "pinFullscreened": False,
        "fullscreen": 0,
        "fullscreenClient": 0,
        "fullscreenHandler": "",
        "allowedOverFullscreen": False,
        "grouped": [],
        "tags": [],
        "swallowing": "0x0",
        "focusHistoryID": focus_history_id,
        "inhibitingIdle": False,
        "xdgTag": "",
        "xdgDescription": "",
        "contentType": "none",
        "tearingHint": False,
        "stableId": stable_id or address.removeprefix("0x")[-8:],
    }


def make_monitor(name: str = "eDP-1", *, monitor_id: int = 0, active_workspace: int = 1) -> dict[str, Any]:
    return {
        "id": monitor_id,
        "name": name,
        "description": "Fake Panel",
        "make": "Fake",
        "model": "Panel",
        "serial": "",
        "width": 2560,
        "height": 1440,
        "physicalWidth": 340,
        "physicalHeight": 190,
        "refreshRate": 165.0,
        "x": 0,
        "y": 0,
        "activeWorkspace": {"id": active_workspace, "name": str(active_workspace)},
        "specialWorkspace": {"id": 0, "name": ""},
        "reserved": [0, 0, 0, 0],
        "scale": 1.6,
        "transform": 0,
        "focused": True,
        "dpmsStatus": True,
        "vrr": False,
        "disabled": False,
    }


class FakeHyprland:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        signature: str = "fake_1700000000_1234567",
        version: str = "0.56.2",
        config_provider: Literal["lua", "hyprlang"] | None = "lua",
    ) -> None:
        self.runtime_dir = runtime_dir
        self.signature = signature
        self.version = version
        # None simulates Hyprland < 0.55, which has no `status` request.
        self.config_provider = config_provider
        self.windows: list[dict[str, Any]] = []
        self.monitors: list[dict[str, Any]] = [make_monitor()]
        self.active_address: str | None = None
        self.requests: list[str] = []
        self.dispatch_handler: Callable[[str], str] | None = None
        self._servers: list[asyncio.Server] = []
        self._event_writers: set[asyncio.StreamWriter] = set()
        self._clients_changed = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    @property
    def directory(self) -> Path:
        return self.runtime_dir / "hypr" / self.signature

    @property
    def instance(self) -> HyprlandInstance:
        return HyprlandInstance(self.signature, self.directory, "environment")

    @property
    def env(self) -> dict[str, str]:
        return {"XDG_RUNTIME_DIR": str(self.runtime_dir), "HYPRLAND_INSTANCE_SIGNATURE": self.signature}

    async def start(self) -> HyprlandInstance:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._servers.append(
            await asyncio.start_unix_server(
                self._serve_request, path=str(self.directory / REQUEST_SOCKET_NAME)
            )
        )
        self._servers.append(
            await asyncio.start_unix_server(self._serve_events, path=str(self.directory / EVENT_SOCKET_NAME))
        )
        return self.instance

    async def stop(self) -> None:
        for writer in list(self._event_writers):
            writer.close()
        self._event_writers.clear()
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    # ------------------------------------------------------------------ events
    async def emit(self, name: str, data: str = "") -> None:
        line = f"{name}>>{data[:1024]}".replace("\n", " ") + "\n"
        for writer in list(self._event_writers):
            try:
                writer.write(line.encode("utf-8"))
                await writer.drain()
            except (ConnectionError, OSError):
                self._event_writers.discard(writer)

    async def wait_for_event_clients(self, count: int = 1, timeout_s: float = 2.0) -> None:
        async with asyncio.timeout(timeout_s):
            while len(self._event_writers) < count:
                self._clients_changed.clear()
                await self._clients_changed.wait()

    async def _serve_events(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._event_writers.add(writer)
        self._clients_changed.set()
        with contextlib.suppress(ConnectionError, OSError):
            await reader.read()  # park until the client disconnects
        self._event_writers.discard(writer)
        writer.close()

    # ------------------------------------------------------------------ requests
    async def _serve_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = b""
            while True:  # Hyprland's read loop: 1023-byte reads, stop on a short read
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK), timeout=5)
                request += chunk
                if len(chunk) < _READ_CHUNK:
                    break
            text = request.decode("utf-8", errors="replace")
            self.requests.append(text)
            writer.write(self.reply_for(text).encode("utf-8"))
            await writer.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def reply_for(self, request: str) -> str:
        as_json = False
        command = request
        head = request.split(" ", 1)[0]
        if not request.startswith("[[BATCH]]") and "/" in head:
            flags, command = request.split("/", 1)
            as_json = "j" in flags

        if command == "version":
            return self._version_reply(as_json)
        if command == "status":
            if self.config_provider is None:
                return "unknown request"
            payload = {"configProvider": self.config_provider, "backend": "drm"}
            return (
                json.dumps(payload) if as_json else f"configProvider: {self.config_provider}\nbackend: drm\n"
            )
        if command == "clients":
            return json.dumps(self.windows)
        if command == "workspaces":
            return json.dumps(self._workspaces())
        if command == "monitors":
            return json.dumps(self.monitors)
        if command == "activewindow":
            active = next((w for w in self.windows if w["address"] == self.active_address), None)
            return json.dumps(active) if active else ("{}" if as_json else "Invalid")
        if command.startswith("dispatch "):
            argument = command.removeprefix("dispatch ")
            return self.dispatch_handler(argument) if self.dispatch_handler else "ok"
        return "unknown request"

    def _version_reply(self, as_json: bool) -> str:
        if not as_json:
            return (
                f"Hyprland {self.version} built from branch v{self.version} (fake).\nTag: v{self.version}\n"
            )
        return json.dumps(
            {
                "branch": f"v{self.version}",
                "commit": "0" * 40,
                "version": self.version,
                "dirty": False,
                "commit_message": "fake",
                "commit_date": "Wed Aug 5 14:13:21 2026",
                "tag": f"v{self.version}",
                "commits": "7661",
            }
        )

    def _workspaces(self) -> list[dict[str, Any]]:
        by_id: dict[int, dict[str, Any]] = {}
        for window in self.windows:
            ref = window["workspace"]
            entry = by_id.setdefault(
                ref["id"],
                {
                    "id": ref["id"],
                    "name": ref["name"],
                    "monitor": self.monitors[0]["name"] if self.monitors else "?",
                    "monitorID": 0,
                    "windows": 0,
                    "hasfullscreen": False,
                    "lastwindow": window["address"],
                    "lastwindowtitle": window["title"],
                    "ispersistent": False,
                    "tiledLayout": "dwindle",
                },
            )
            entry["windows"] += 1
        for monitor in self.monitors:
            active = monitor["activeWorkspace"]
            by_id.setdefault(
                active["id"],
                {
                    "id": active["id"],
                    "name": active["name"],
                    "monitor": monitor["name"],
                    "monitorID": monitor["id"],
                    "windows": 0,
                    "hasfullscreen": False,
                    "lastwindow": "0x0",
                    "lastwindowtitle": "",
                    "ispersistent": False,
                    "tiledLayout": "dwindle",
                },
            )
        return [by_id[key] for key in sorted(by_id)]
