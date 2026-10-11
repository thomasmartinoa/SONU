"""A fake browser with SONU's extension (D70, D71), for tests and evals: a socket like the native
messaging host's, answering the same JSON lines about its tabs and their pages."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FakeTab:
    id: int
    title: str
    url: str
    window: int = 1
    active: bool = False
    text: str = ""  # the page's visible text
    selection: str = ""
    readable: bool = True  # False: a page browsers keep extensions out of (settings, the add-on store)


@dataclass
class FakeBrowser:
    folder: Path
    name: str = "zen"
    tabs: list[FakeTab] = field(default_factory=list)
    on_activate: Callable[[FakeTab], None] | None = None  # e.g. the fake desktop's window title changes
    requests: list[dict[str, Any]] = field(default_factory=list)
    version: str = "0.2.0"  # "0.1.0": the first extension, which can't read pages
    _server: asyncio.Server | None = None

    @property
    def path(self) -> Path:
        return self.folder / f"{self.name}-1.sock"

    async def __aenter__(self) -> FakeBrowser:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        self.path.unlink(missing_ok=True)

    def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "hello":
            return {"browser": self.name, "version": self.version}
        if op == "tabs":
            return {
                "tabs": [
                    {
                        "id": t.id,
                        "window": t.window,
                        "index": i,
                        "active": t.active,
                        "title": t.title,
                        "url": t.url,
                    }
                    for i, t in enumerate(self.tabs)
                ],
                "focused_window": 1,
            }
        if op == "activate":
            tab = next(t for t in self.tabs if t.id == request["tab"])
            for other in self.tabs:
                other.active = other is tab
            if self.on_activate is not None:
                self.on_activate(tab)
            return {"tab": {"id": tab.id}}
        if op == "close":
            self.tabs = [t for t in self.tabs if t.id not in request["tabs"]]
            return {"closed": len(request["tabs"])}
        if op == "page" and self.version != "0.1.0":
            tab = next(t for t in self.tabs if t.id == request["tab"])
            if not tab.readable:
                raise ValueError("Cannot access contents of the page")
            limit = int(request.get("limit") or 20000)
            return {
                "title": tab.title,
                "url": tab.url,
                "text": tab.text[:limit],
                "length": len(tab.text),
                "selection": tab.selection,
            }
        raise ValueError(f"unknown op {op}")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        self.requests.append(request)
        try:
            reply = {"ok": True, "result": self._answer(request)}
        except (ValueError, StopIteration, KeyError) as exc:
            reply = {"ok": False, "error": str(exc)}
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()
