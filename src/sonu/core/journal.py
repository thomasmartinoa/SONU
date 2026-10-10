"""The activity journal (D6): which window had the focus, and when — "what was I doing yesterday?".

* Recorded by sonud from Hyprland's events: each time the focus moves or the focused window's title
  changes, one row (time, window class, title). A window keeps the time until the next row, but at most
  ``MAX_SPAN_S``: Hyprland doesn't say when the user walks away, so an hour on one window counts as
  half an hour, never as a whole night.
* Local only: a SQLite file in SONU's data directory (0600), rows older than ``keep_days`` deleted.
  Password managers, private/incognito browser windows and the apps the user lists are never written —
  the time in them only ends the window before. ``sonu journal pause`` (or saying it) stops recording
  until it's resumed; ``forget`` deletes rows.
* Titles are what apps and web pages wrote: data, never instructions — a summary is a private result
  (kept out of the audit log and the model, read out by SONU itself).
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sonu.desktop.hyprland.events import HyprlandEvent

MAX_SPAN_S = 30 * 60
COALESCE_S = 2.0  # a title changing faster than this replaces the row before (loading pages)
PRUNE_EVERY_S = 3600.0

# Never written (window class, lower case, contains one of these).
PASSWORD_MANAGERS = (
    "keepass",
    "bitwarden",
    "1password",
    "enpass",
    "seahorse",
    "kwallet",
    "proton-pass",
    "protonpass",
    "passwordsafe",
    "secrets",
)
_PRIVATE_TITLE = re.compile(r"private browsing|\bincognito\b|\binprivate\b|private window", re.I)


def title_text(title: str) -> str:
    """What a title says, without a spinner or an unread count in front ("◑ Fix tests", "(3) Inbox"):
    found on the laptop — Claude Code's spinner changed its terminal's title every second."""
    return re.sub(r"^(?:[\W_]*?\(\d+\+?\))?[\W_]*", "", title)


def title_key(title: str) -> str:
    return title_text(title).casefold()


def excluded(app: str, title: str, more_apps: Iterable[str] = ()) -> bool:
    lowered = app.casefold()
    if any(name in lowered for name in PASSWORD_MANAGERS):
        return True
    if any(name.casefold() and name.casefold() in lowered for name in more_apps):
        return True
    return bool(_PRIVATE_TITLE.search(title))


@dataclass(frozen=True, slots=True)
class Span:
    app: str  # window class
    title: str
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


class Journal:
    def __init__(
        self,
        path: Path | None,
        *,
        keep_days: float = 30,
        exclude_apps: Iterable[str] = (),
        pause_file: Path | None = None,
    ) -> None:
        self.path, self.keep_days = path, keep_days
        self.exclude_apps = tuple(exclude_apps)
        self.pause_file = pause_file
        self._paused_here = False  # in memory only (tests), when there's no pause file
        self._last: tuple[int, str, str, float] | None = None  # rowid, app, title, at
        self._pruned = 0.0
        self._db: sqlite3.Connection | None = None

    # -------------------------------------------------------------- the file
    def _connect(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        if self.path is None:
            self._db = sqlite3.connect(":memory:")
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
            self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS focus (at REAL NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS focus_at ON focus (at)")
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # -------------------------------------------------------------- pausing
    @property
    def paused(self) -> bool:
        return self.pause_file.exists() if self.pause_file is not None else self._paused_here

    def pause(self, on: bool, at: float | None = None) -> None:
        if on:
            self.record("", "", at)  # the window before stops counting now
        if self.pause_file is None:
            self._paused_here = on
        elif on:
            self.pause_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.pause_file.write_text("paused\n")
        else:
            self.pause_file.unlink(missing_ok=True)

    # -------------------------------------------------------------- writing
    def record(self, app: str, title: str, at: float | None = None) -> None:
        """The focus moved to (app, title); ("", "") is nothing — no window, paused or excluded."""
        if self.paused:
            return
        at = time.time() if at is None else at
        if app and excluded(app, title, self.exclude_apps):
            app = title = ""
        title = " ".join(title.split())[:300]
        db = self._connect()
        last = self._last
        if last is not None and (last[1], last[2]) == (app, title):
            return
        same_title = last is not None and title_key(last[2]) == title_key(title)
        if last is not None and last[1] == app and app and (same_title or at - last[3] < COALESCE_S):
            db.execute("UPDATE focus SET title = ? WHERE rowid = ?", (title, last[0]))
            self._last = (last[0], app, title, last[3])
        else:
            if last is None and not app:  # nothing before it to end
                return
            cursor = db.execute("INSERT INTO focus (at, app, title) VALUES (?, ?, ?)", (at, app, title))
            self._last = (int(cursor.lastrowid or 0), app, title, at)
        db.commit()
        if at - self._pruned > PRUNE_EVERY_S:
            self.prune(at)

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        db = self._connect()
        db.execute("DELETE FROM focus WHERE at < ?", (now - self.keep_days * 86400,))
        db.commit()
        self._pruned = now

    def forget(self, since: float) -> int:
        """Delete everything recorded since then; returns how many entries went."""
        db = self._connect()
        count = db.execute("DELETE FROM focus WHERE at >= ?", (since,)).rowcount
        db.commit()
        self._last = None
        return count

    def count(self, since: float = 0.0) -> int:
        row = (
            self._connect()
            .execute("SELECT COUNT(*) FROM focus WHERE at >= ? AND app != ''", (since,))
            .fetchone()
        )
        return int(row[0])

    # -------------------------------------------------------------- reading
    def spans(self, start: float, end: float, now: float | None = None) -> list[Span]:
        """The windows that had the focus between start and end, each clipped to that time."""
        now = time.time() if now is None else now
        rows = (
            self._connect()
            .execute(
                "SELECT at, app, title FROM focus WHERE at >= ? AND at < ? ORDER BY at, rowid",
                (start - MAX_SPAN_S, end),
            )
            .fetchall()
        )
        later = self._connect().execute("SELECT MIN(at) FROM focus WHERE at >= ?", (end,)).fetchone()[0]
        spans: list[Span] = []
        for index, (at, app, title) in enumerate(rows):
            following = rows[index + 1][0] if index + 1 < len(rows) else (later if later is not None else now)
            stop = min(following, at + MAX_SPAN_S, end, now)
            begin = max(at, start)
            if app and stop > begin:
                spans.append(Span(app, title, begin, stop))
        return spans


class FocusTracker:
    """Turns Hyprland's events into journal rows (sonud feeds it the event stream)."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.address: str | None = None
        self.app = ""

    def start(self, app: str, title: str, address: str | None) -> None:
        """What has the focus when recording starts."""
        self.app, self.address = app, address
        self.journal.record(app, title)

    def feed(self, event: HyprlandEvent) -> None:
        if event.name == "activewindow":
            app, title = (*event.fields, "", "")[:2]
            self.app = app
            self.journal.record(app, title)
        elif event.name == "activewindowv2":
            self.address = event.window_address
        elif event.name == "windowtitlev2" and self.address and event.window_address == self.address:
            if len(event.fields) == 2:
                self.journal.record(self.app, event.fields[1])

    def stop(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.journal.record("", "")
