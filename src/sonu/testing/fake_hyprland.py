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
import shlex
import zlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from sonu.desktop.hyprland.instance import EVENT_SOCKET_NAME, REQUEST_SOCKET_NAME, HyprlandInstance
from sonu.testing.fake_dispatch import FakeCommand, parse_dispatch

_READ_CHUNK = 1023
# Above Linux's largest pid_max (2^22), so a fake app's process is never a real one — in particular
# never an ancestor of the test process (see sonu.world.origin).
FAKE_PID_BASE = 5_000_000


def app_pid(window_class: str) -> int:
    """The fake process behind all of an app's windows (one process per app, like a browser)."""
    return FAKE_PID_BASE + zlib.crc32(window_class.encode()) % 1_000_000


def _start_folder(argv: list[str]) -> str | None:
    """The folder a terminal is told to start in (not SONU's own terminals: they run a program)."""
    if "--listen-on" in argv:
        return None
    for index, arg in enumerate(argv[:-1]):
        if arg in ("--directory", "--working-directory", "--cwd", "--workdir"):
            return argv[index + 1]
    return next((a.split("=", 1)[1] for a in argv if a.startswith("--working-directory=")), None)


def _toggled(current: bool, action: str | None) -> bool:
    return True if action == "enable" else False if action == "disable" else not current


def make_window(
    address: str,
    window_class: str,
    title: str = "",
    *,
    workspace_id: int = 1,
    workspace_name: str | None = None,
    monitor: int = 0,
    pid: int = 0,  # 0: never an ancestor of the test process (see sonu.world.origin)
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


@dataclass
class FakeApp:
    """How a fake application behaves when launched — including the ways real apps misbehave."""

    window_class: str
    title: str = ""
    delay_s: float = 0.05
    # A second launch hands off to the running instance (VS Code, Chromium): exec rules are lost.
    single_instance: bool = False
    # False: ignores exec workspace rules and opens on the focused workspace.
    honours_rules: bool = True
    # Force the window onto this workspace no matter what (simulates stubborn apps).
    lands_on: int | None = None
    # False: the process starts but never maps a window.
    opens_window: bool = True
    # False: ignores close requests (e.g. an "unsaved changes" dialog keeps it open).
    closes: bool = True
    # Browsers: a link on the command line opens in the running browser's most recent window (a new
    # tab) and its title becomes "<url> - <title>". False: the page never loads (title unchanged).
    shows_pages: bool = True
    # Seconds a page takes to load after the tab opens (the title first reads "Loading…").
    page_load_s: float = 0.0


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
        # Called with every http(s) link a fake browser is started with, and the browser's window
        # class (e.g. FakeSystem.page_opened).
        self.on_url: Callable[[str, str], None] | None = None
        self.on_exec: Callable[[list[str]], None] | None = None  # e.g. FakeKitty.launched
        # A /proc for terminals started with a folder (--directory, --working-directory…): their shell's
        # working folder, as SystemControl.child_folders reads it. None: not written.
        self.proc: Path | None = None
        self._next_child = 900_000
        self.cursor: tuple[int, int] = (0, 0)
        self.requests: list[str] = []
        self.dispatch_handler: Callable[[str], str] | None = None
        # Launch simulation: argv[0] basename -> behaviour. Unknown commands open nothing.
        self.apps: dict[str, FakeApp] = {}
        self.launched: list[list[str]] = []
        # True: fullscreen/float/pin requests answer "ok" but change nothing (as for a window that refuses).
        self.ignore_state = False
        self.exited = False  # the session was ended (log out)
        self._next_address = 0x5500_0000_1000
        self._tasks: set[asyncio.Task[None]] = set()
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
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
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
            if self.dispatch_handler:
                return self.dispatch_handler(argument)
            return self._simulate_dispatch(argument)
        return "unknown request"

    # ------------------------------------------------------------------ simulation
    @property
    def active_workspace(self) -> int:
        return int(self.monitors[0]["activeWorkspace"]["id"])

    def window(self, address: str) -> dict[str, Any] | None:
        return next((w for w in self.windows if w["address"] == address), None)

    def windows_of_class(self, window_class: str) -> list[dict[str, Any]]:
        return [w for w in self.windows if w["class"] == window_class]

    def _find(self, selector: str | None) -> dict[str, Any] | None:
        if selector is None:  # real Hyprland: no selector means "the focused window"
            return self.window(self.active_address) if self.active_address else None
        kind, _, value = selector.partition(":")
        if kind == "address":
            return self.window(value.lower())
        if kind == "stableid":
            return next((w for w in self.windows if w["stableId"] == value.lower()), None)
        return None

    def _background(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _simulate_dispatch(self, argument: str) -> str:
        provider = self.config_provider or "hyprlang"
        parsed = parse_dispatch(argument, provider)
        if parsed is None:
            return f"error: fake Hyprland cannot parse dispatch {argument!r}"
        if parsed.kind == "exec":
            return self._exec(parsed)
        if parsed.kind == "exit":
            self.exited = True
            return "ok"
        if parsed.kind == "cursor":
            self.cursor = (parsed.x or 0, parsed.y or 0)
            return "ok"
        if parsed.kind == "focus_workspace":
            self._background(self._focus_workspace(int(parsed.workspace or "1")))
            return "ok"
        window = self._find(parsed.window)
        if window is None:
            return "ok"  # real Hyprland: a selector that matches nothing is a silent no-op
        if parsed.kind == "move":
            self._background(self._move(window, int(parsed.workspace or "1")))
        elif parsed.kind == "focus_window":
            self._background(self._focus_window(window))
        elif parsed.kind == "close":
            self._background(self._close(window))
        elif parsed.kind in ("fullscreen", "float", "pin") and not self.ignore_state:
            self._background(self._set_state(window, parsed))
        return "ok"

    async def _set_state(self, window: dict[str, Any], parsed: FakeCommand) -> None:
        """Fullscreen, float and pin, with Hyprland 0.56.2's rules (ConfigActions.cpp)."""
        await asyncio.sleep(0.01)
        address = window["address"].removeprefix("0x")
        if parsed.kind == "fullscreen":
            mode = 1 if parsed.mode == "maximized" else 2
            if parsed.action == "set":
                window["fullscreen"] = mode
            elif window["fullscreen"] == mode:  # unset only leaves the mode it's in
                window["fullscreen"] = 0
            await self.emit("fullscreen", "1" if window["fullscreen"] else "0")
        elif parsed.kind == "float":
            window["floating"] = _toggled(window["floating"], parsed.action)
            if window["floating"]:
                window["at"], window["size"] = [640, 360], [1280, 720]
            await self.emit("changefloatingmode", f"{address},{int(window['floating'])}")
        else:
            # Only a floating window that isn't fullscreen can be pinned; it shows on the active workspace.
            if not window["floating"] or window["fullscreen"]:
                return
            window["pinned"] = _toggled(window["pinned"], parsed.action)
            if window["pinned"]:
                window["workspace"] = {"id": self.active_workspace, "name": str(self.active_workspace)}
            await self.emit("pin", f"{address},{int(window['pinned'])}")
        self.retile()

    def retile(self) -> None:
        """A simple tiler: each workspace's tiled windows share the monitor in equal columns."""
        width, height = self.monitors[0]["width"], self.monitors[0]["height"]
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for window in self.windows:
            if window["fullscreen"]:
                window["at"], window["size"] = [0, 0], [width, height]
            elif not window["floating"]:
                by_workspace.setdefault(window["workspace"]["name"], []).append(window)
        for tiled in by_workspace.values():
            column = width // len(tiled)
            for index, window in enumerate(tiled):
                window["at"], window["size"] = [index * column, 0], [column, height]

    def _exec(self, parsed: FakeCommand) -> str:
        argv = shlex.split(parsed.command or "")
        if not argv:
            return "error: empty command"
        self.launched.append(argv)
        if self.on_exec is not None:
            self.on_exec(argv)
        app = self.apps.get(Path(argv[0]).name)
        if app is None or not app.opens_window:
            return "ok"  # real Hyprland reports ok even if the program fails to start
        if self.proc is not None and (folder := _start_folder(argv)) is not None:
            self._shell_in(app_pid(app.window_class), folder)
        rule_workspace = int(parsed.workspace) if parsed.workspace else None
        url = next((arg for arg in argv[1:] if arg.startswith(("https://", "http://"))), None)
        if url and self.on_url is not None:
            self.on_url(url, app.window_class)
        running = sorted(self.windows_of_class(app.window_class), key=lambda w: w["focusHistoryID"])
        # A folder or file on the command line: the window shows its name, like Thunar or an image viewer.
        path = next((arg for arg in argv[1:] if arg.startswith("/")), None)
        if url and running:
            self._background(self._load_page(app, running[0], url))
        else:
            title = f"{url} - {app.title}" if url and app.shows_pages else None
            if "--title" in argv[:-1]:  # kitty --title …
                title = argv[argv.index("--title") + 1]
            if title is None and path and app.shows_pages:
                title = f"{Path(path).name} - {app.title}"
            if path and "--goto" in argv[:-1]:  # VS Code: "planner.py - SONU - Visual Studio Code"
                file = argv[argv.index("--goto") + 1].rsplit(":", 1)[0]
                title = f"{Path(file).name} - {Path(path).name} - {app.title}"
            self._background(self._open(app, rule_workspace, title))
        return "ok"

    def _shell_in(self, pid: int, folder: str) -> None:
        if self.proc is None:
            return
        self._next_child += 1
        children = self.proc / str(pid) / "task" / str(pid) / "children"
        children.parent.mkdir(parents=True, exist_ok=True)
        before = children.read_text() if children.exists() else ""
        children.write_text(f"{before}{self._next_child} ")
        shell = self.proc / str(self._next_child)
        shell.mkdir(parents=True)
        (shell / "cwd").symlink_to(folder)

    async def _load_page(self, app: FakeApp, window: dict[str, Any], url: str) -> None:
        await asyncio.sleep(app.delay_s)
        if not app.shows_pages:
            return
        address = window["address"].removeprefix("0x")
        if app.page_load_s:
            window["title"] = f"Loading… - {app.title}"
            await self.emit("windowtitlev2", f"{address},{window['title']}")
            await asyncio.sleep(app.page_load_s)
        window["title"] = f"{url} - {app.title}"
        await self.emit("windowtitlev2", f"{address},{window['title']}")

    async def _open(self, app: FakeApp, rule_workspace: int | None, title: str | None = None) -> None:
        await asyncio.sleep(app.delay_s)
        handed_off = app.single_instance and bool(self.windows_of_class(app.window_class))
        if app.lands_on is not None:
            workspace = app.lands_on
        elif rule_workspace is not None and app.honours_rules and not handed_off:
            workspace = rule_workspace
        else:
            workspace = self.active_workspace
        address = f"0x{self._next_address:x}"
        self._next_address += 0x100
        title = title if title is not None else app.title
        window = make_window(
            address, app.window_class, title, workspace_id=workspace, pid=app_pid(app.window_class)
        )
        self.windows.append(window)
        self.retile()
        await self.emit("openwindow", f"{address.removeprefix('0x')},{workspace},{app.window_class},{title}")
        if workspace == self.active_workspace:
            self.active_address = address
            await self.emit("activewindow", f"{app.window_class},{title}")
            await self.emit("activewindowv2", address.removeprefix("0x"))

    async def _move(self, window: dict[str, Any], workspace: int) -> None:
        window["workspace"] = {"id": workspace, "name": str(workspace)}
        self.retile()
        if self.active_address == window["address"] and workspace != self.active_workspace:
            self.active_address = None
        await self.emit("movewindowv2", f"{window['address'].removeprefix('0x')},{workspace},{workspace}")

    async def _focus_workspace(self, workspace: int) -> None:
        self.monitors[0]["activeWorkspace"] = {"id": workspace, "name": str(workspace)}
        await self.emit("workspacev2", f"{workspace},{workspace}")

    async def _focus_window(self, window: dict[str, Any]) -> None:
        workspace = int(window["workspace"]["id"])
        if workspace != self.active_workspace:
            await self._focus_workspace(workspace)
        self.active_address = window["address"]
        await self.emit("activewindow", f"{window['class']},{window['title']}")
        await self.emit("activewindowv2", window["address"].removeprefix("0x"))

    async def _close(self, window: dict[str, Any]) -> None:
        app = next((a for a in self.apps.values() if a.window_class == window["class"]), None)
        if app is not None and not app.closes:
            return
        await asyncio.sleep(0.01)
        if window in self.windows:
            self.windows.remove(window)
        if self.active_address == window["address"]:
            self.active_address = None
        self.retile()
        await self.emit("closewindow", window["address"].removeprefix("0x"))

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
