"""The latest notifications, for "what was that notification?" (D69).

Off unless the user turns it on (``[notifications] remember = true``): notifications can carry message
previews. Then sonud watches the session bus for notifications being shown (``busctl --user monitor``,
the calls to ``org.freedesktop.Notifications.Notify``) and keeps the latest few **in memory only** —
never on disk, gone when sonud stops, dropped after ``keep_minutes``. Password managers, the apps the
user lists and SONU's own messages (except its reminders) are never kept. What's kept is shown and
said only to the user: a private result, never given to a model or written to the audit log.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import re
import signal
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sonu.core.journal import PASSWORD_MANAGERS

logger = logging.getLogger(__name__)

MAX_KEPT = 30
MAX_SUMMARY = 120
MAX_BODY = 300
WATCH_ARGV = (
    "busctl",
    "--user",
    "monitor",
    "--json=short",
    "--match",
    "type=method_call,interface=org.freedesktop.Notifications,member=Notify",
)
SONU_REMINDER = "SONU reminder"  # SONU's own notifications are kept only when they're reminders
_TAG = re.compile(r"<[^>]{0,200}>")


@dataclass(frozen=True, slots=True)
class Notification:
    app: str
    summary: str
    body: str
    at: float

    def describe(self, now: float) -> str:
        minutes = int((now - self.at) // 60)
        ago = "just now" if minutes < 1 else f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        text = f"{self.summary} — {self.body}" if self.body and self.summary else self.summary or self.body
        return f"{self.app or 'An app'}, {ago}: {text}"


def _clean(text: str, limit: int) -> str:
    """Notification bodies may carry simple markup (<b>, <a href>): the text only, on one line."""
    plain = " ".join(html.unescape(_TAG.sub("", text)).split())
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


class NotificationLog:
    def __init__(self, keep_minutes: int = 60, exclude_apps: Iterable[str] = ()) -> None:
        self.keep_s = keep_minutes * 60
        self.exclude = [name.casefold() for name in exclude_apps if name.strip()]
        self._items: deque[Notification] = deque(maxlen=MAX_KEPT)

    def add(self, app: str, summary: str, body: str, at: float | None = None) -> bool:
        """Keep it unless it's one never kept; whether it was kept."""
        lowered = app.casefold()
        if any(name in lowered for name in (*PASSWORD_MANAGERS, *self.exclude)):
            return False
        if lowered == "sonu" and summary != SONU_REMINDER:
            return False  # SONU's own status messages
        item = Notification(
            _clean(app, 40), _clean(summary, MAX_SUMMARY), _clean(body, MAX_BODY), at or time.time()
        )
        if not item.summary and not item.body:
            return False
        last = self._items[-1] if self._items else None
        if last is not None and (last.app, last.summary, last.body) == (item.app, item.summary, item.body):
            return False  # the same one shown again (an update)
        self._items.append(item)
        return True

    def feed(self, line: str | bytes) -> bool:
        """One line of ``busctl monitor --json=short``."""
        try:
            message = json.loads(line)
            data = message["payload"]["data"]
            if message.get("member") != "Notify" or len(data) < 5:
                return False
            app, summary, body = data[0], data[3], data[4]
        except (ValueError, KeyError, TypeError):
            return False
        if not all(isinstance(value, str) for value in (app, summary, body)):
            return False
        realtime = message.get("timestamp-realtime")
        at = realtime / 1_000_000 if isinstance(realtime, int) else None
        return self.add(app, summary, body, at)

    def recent(self, now: float | None = None) -> list[Notification]:
        """The ones still kept, newest first (older than keep_minutes are dropped)."""
        now = time.time() if now is None else now
        while self._items and now - self._items[0].at > self.keep_s:
            self._items.popleft()
        return list(reversed(self._items))

    def forget(self) -> int:
        count = len(self._items)
        self._items.clear()
        return count


async def watch(log: NotificationLog, env: Mapping[str, str], retry_s: float = 10.0) -> None:
    """Feed ``log`` from the session bus for as long as it runs (busctl restarted if it stops)."""
    while True:
        try:
            process = await asyncio.create_subprocess_exec(
                *WATCH_ARGV,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=dict(env),
                limit=1024 * 1024,
                start_new_session=True,  # stopped as a group: nothing it started keeps the pipe open
            )
        except OSError as exc:
            logger.warning("notifications: couldn't start busctl: %s", exc)
            await asyncio.sleep(retry_s)
            continue
        stdout = process.stdout  # a pipe: never None
        try:
            while stdout is not None and (line := await stdout.readline()):
                log.feed(line)
        except (ValueError, OSError) as exc:  # a line over the limit, the pipe broke
            logger.info("notifications: %s", exc)
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if stdout is not None:
                with contextlib.suppress(Exception):
                    await stdout.read()  # to the end: the pipe closes with the process
        await asyncio.sleep(retry_s)
