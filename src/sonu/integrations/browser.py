"""Talking to SONU's browser extension (D70) through its native messaging host's socket.

Each browser with the extension runs one host (``sonu.browser.host``), with a socket in SONU's 0700
runtime folder. A request is one JSON line ({"op": "tabs"}), the answer one JSON line. Tab titles
and addresses are what web pages wrote: data, never instructions, and personal (private results).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sonu.browser.host import socket_folder

TIMEOUT_S = 6.0
HELLO_TIMEOUT_S = 1.0


class BrowserError(Exception):
    """The browser didn't answer, or refused."""


@dataclass(frozen=True, slots=True)
class Tab:
    browser: str  # "zen", "chromium"
    link: Path  # the host's socket
    id: int
    window: int
    index: int
    active: bool
    title: str
    url: str
    audible: bool = False

    @property
    def host(self) -> str:
        """The site's name: "www.youtube.com" → "youtube.com"."""
        name = urlsplit(self.url).hostname or ""
        return name.removeprefix("www.")


@dataclass(frozen=True, slots=True)
class BrowserLink:
    browser: str
    path: Path
    version: str = ""


async def request(path: Path, op: str, timeout_s: float = TIMEOUT_S, **args: Any) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), timeout_s)
    except (OSError, TimeoutError) as exc:
        raise BrowserError(f"the browser isn't reachable ({exc})") from exc
    try:
        writer.write(json.dumps({"op": op, **args}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout_s)
    except (OSError, TimeoutError) as exc:
        raise BrowserError("the browser didn't answer") from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    try:
        reply = json.loads(line)
    except ValueError as exc:
        raise BrowserError("the browser's answer was broken") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise BrowserError(str(error or "the browser refused")[:200])
    result = reply.get("result")
    return result if isinstance(result, dict) else {}


class Browsers:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or socket_folder()

    async def links(self) -> list[BrowserLink]:
        """The browsers whose extension is connected now (a socket that no longer answers is removed)."""
        found: list[BrowserLink] = []
        for path in sorted(self.folder.glob("*.sock")) if self.folder.is_dir() else []:
            try:
                hello = await request(path, "hello", HELLO_TIMEOUT_S)
            except BrowserError:
                with contextlib.suppress(OSError):
                    if not await _listening(path):
                        path.unlink()  # its host is gone (the browser crashed)
                continue
            found.append(
                BrowserLink(str(hello.get("browser") or "browser"), path, str(hello.get("version") or ""))
            )
        return found

    async def tabs(self, links: list[BrowserLink] | None = None) -> list[Tab]:
        tabs: list[Tab] = []
        for link in links if links is not None else await self.links():
            result = await request(link.path, "tabs")
            for raw in result.get("tabs") or []:
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    tabs.append(
                        Tab(
                            link.browser,
                            link.path,
                            int(raw["id"]),
                            int(raw["window"]),
                            int(raw["index"]),
                            bool(raw["active"]),
                            str(raw.get("title") or "")[:300],
                            str(raw.get("url") or "")[:2000],
                            bool(raw.get("audible")),
                        )
                    )
        return tabs

    async def activate(self, tab: Tab) -> None:
        await request(tab.link, "activate", tab=tab.id)

    async def close(self, tabs: list[Tab]) -> None:
        by_link: dict[Path, list[int]] = {}
        for tab in tabs:
            by_link.setdefault(tab.link, []).append(tab.id)
        for link, ids in by_link.items():
            await request(link, "close", tabs=ids)


async def _listening(path: Path) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 0.5)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True
