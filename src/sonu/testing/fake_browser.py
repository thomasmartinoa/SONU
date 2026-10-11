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
class FakeControl:
    """A link, button, checkbox or field on a fake page (D72)."""

    name: str
    kind: str = "button"  # "link", "button", "checkbox", "field"
    goes_to: str = ""  # a link: the address it opens (the tab's address and title change)
    secret: bool = False  # a password field
    in_view: bool = True
    inert: bool = False  # a button that does nothing the page shows
    submits: bool = False  # a field: Enter shows results (the title changes)
    checked: bool = False
    value: str = ""


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
    controls: list[FakeControl] = field(default_factory=list)
    scrolled: float = 0.0
    height: float = 3000.0  # how far the page scrolls (0: it doesn't)


@dataclass
class FakeBrowser:
    folder: Path
    name: str = "zen"
    tabs: list[FakeTab] = field(default_factory=list)
    on_activate: Callable[[FakeTab], None] | None = None  # e.g. the fake desktop's window title changes
    requests: list[dict[str, Any]] = field(default_factory=list)
    version: str = "0.3.0"  # "0.1.0": the first extension (no pages); "0.2.0": reads pages, can't act
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
        if op in ("controls", "click", "state", "fill", "scroll") and self.version not in ("0.1.0", "0.2.0"):
            tab = next(t for t in self.tabs if t.id == request["tab"])
            if not tab.readable:
                raise ValueError("Cannot access contents of the page")
            return self._act(tab, op, request.get("args") or {})
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

    @staticmethod
    def _numbered(tab: FakeTab, kind: str) -> list[tuple[int, FakeControl]]:
        wanted = [c for c in tab.controls if (c.kind == "field") == (kind == "fill")]
        return list(enumerate(wanted, start=1))

    def _act(self, tab: FakeTab, op: str, args: dict[str, Any]) -> dict[str, Any]:
        page = {"url": tab.url, "title": tab.title, "size": len(tab.text)}
        if op == "controls":
            kind = "fill" if args.get("kind") == "fill" else "click"
            controls = [
                {
                    "ref": ref,
                    "name": c.name,
                    "kind": c.kind,
                    "secret": c.secret,
                    "host": "",
                    "in_view": c.in_view,
                }
                for ref, c in self._numbered(tab, kind)
            ]
            return {"controls": controls, **page}
        if op == "scroll":
            before = tab.scrolled
            step = {"down": 800.0, "up": -800.0}.get(args.get("how", ""), 0.0)
            target = {"top": 0.0, "bottom": tab.height}.get(args.get("how", ""), before + step)
            tab.scrolled = max(0.0, min(tab.height, target))
            return {"before": before, "after": tab.scrolled, "end": tab.height}
        kind = "fill" if op == "fill" else "click"
        found = dict(self._numbered(tab, kind)).get(int(args.get("ref") or 0))
        if op == "state":
            return {**page, "control": {"checked": found.checked} if found else None}
        if found is None:
            return {"found": False}
        if op == "click":
            clicked: dict[str, Any] = {**page, "control": {"checked": found.checked}}
            if found.kind == "link" and found.goes_to:
                tab.url, tab.title, tab.text = found.goes_to, f"{found.name} page", f"The {found.name} page."
            elif found.kind == "checkbox":
                found.checked = not found.checked
            elif not found.inert:
                tab.text += f" {found.name} done."
            return {"found": True, "before": clicked}
        if found.secret:
            return {"found": True, "refused": "a password or card field"}
        found.value = str(args.get("text") or "")
        if args.get("submit"):
            if found.submits:
                tab.title = f"Results for {found.value}"
            return {"found": True, "value": found.value, "submitted": True, "before": page}
        return {"found": True, "value": found.value, "submitted": False}

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
