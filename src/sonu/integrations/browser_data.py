"""The browsers' own history and bookmarks, read from their profile folders (D73): "find the page about
lentil soup I saw yesterday", "open my arch wiki bookmark".

Read-only, and never in place: the browser keeps its database open, so SONU copies it (with Firefox's and
Zen's write-ahead log, which holds the latest visits) into a 0700 folder of its own, reads the copy and
deletes it. Titles and addresses are the user's browsing — private: shown to them only, never given to a
model, the audit log or anything outside the laptop. No extension is needed, and the browser may be closed.

Measured on the owner's laptop (table layouts and counts only): Zen 1.23 ``places.sqlite`` in WAL mode with
a 2 MB ``-wal`` beside it; Chromium 153 ``History`` (rollback journal) and ``Bookmarks`` (JSON, written once
there is a bookmark).
"""

from __future__ import annotations

import configparser
import contextlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sonu.browser.install import CHROMIUM_FOLDERS

Kind = Literal["history", "bookmark"]
_FIREFOX_ROOTS = {
    ".config/zen": "zen",
    ".zen": "zen",
    ".mozilla/firefox": "firefox",
    ".config/mozilla/firefox": "firefox",
}
_CHROMIUM_NAMES = {
    "chromium": "chromium",
    "google-chrome": "chrome",
    "google-chrome-beta": "chrome",
    "BraveSoftware/Brave-Browser": "brave",
    "vivaldi": "vivaldi",
    "microsoft-edge": "edge",
}
_CHROMIUM_EPOCH_S = 11644473600  # 1601-01-01 → 1970-01-01
MAX_ROWS = 200


@dataclass(frozen=True, slots=True)
class Profile:
    browser: str  # "zen", "firefox", "chromium", …
    family: Literal["firefox", "chromium"]
    folder: Path


@dataclass(frozen=True, slots=True)
class Saved:
    """A page from the user's history or bookmarks (personal: never logged or sent)."""

    browser: str
    kind: Kind
    title: str
    url: str
    when: float  # last visit (history) or when it was bookmarked, seconds since 1970; 0: unknown
    visits: int = 0


def profiles(home: Path) -> list[Profile]:
    found: list[Profile] = []
    for root, browser in _FIREFOX_ROOTS.items():
        base = home / root
        ini = configparser.ConfigParser(interpolation=None)
        with contextlib.suppress(OSError, configparser.Error):
            ini.read(base / "profiles.ini", encoding="utf-8")
        for section in ini.sections():
            path = ini.get(section, "Path", fallback="")
            if not path:
                continue
            folder = base / path if ini.get(section, "IsRelative", fallback="1") == "1" else Path(path)
            if (folder / "places.sqlite").is_file():
                found.append(Profile(browser, "firefox", folder))
    for root in CHROMIUM_FOLDERS:
        base = home / ".config" / root
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if (folder.name == "Default" or folder.name.startswith("Profile ")) and (
                (folder / "History").is_file() or (folder / "Bookmarks").is_file()
            ):
                found.append(Profile(_CHROMIUM_NAMES.get(root, root), "chromium", folder))
    # The same folder listed twice (two profiles.ini sections) once.
    return list({p.folder.resolve(): p for p in found}.values())


@contextlib.contextmanager
def _copy(database: Path, scratch: Path) -> Iterator[sqlite3.Connection]:
    """The database as it is now (with its write-ahead log), copied into SONU's folder, deleted after."""
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as folder:
        Path(folder).chmod(0o700)
        copy = Path(folder) / database.name
        shutil.copyfile(database, copy)
        for suffix in ("-wal", "-journal"):
            side = database.with_name(database.name + suffix)
            if side.is_file():
                shutil.copyfile(side, copy.with_name(copy.name + suffix))
        connection = sqlite3.connect(copy)
        try:
            yield connection
        finally:
            connection.close()


def _like(words: list[str], *columns: str) -> tuple[str, list[str]]:
    """SQL: every word in one of the columns (LIKE is case-insensitive for ASCII). The SQL text holds only
    SONU's column names and "?"; the words are parameters."""
    if not words:
        return "1", []
    each = "(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")"
    params: list[str] = []
    for word in words:
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += [f"%{escaped}%"] * len(columns)
    return " AND ".join([each] * len(words)), params


def history(profile: Profile, scratch: Path, words: list[str], since: float, until: float) -> list[Saved]:
    """Pages visited between ``since`` and ``until`` whose title or address has every word, newest first."""
    if profile.family == "firefox":
        where, params = _like(words, "title", "url")
        sql = (
            "SELECT title, url, last_visit_date / 1000000.0, visit_count FROM moz_places "  # noqa: S608
            f"WHERE hidden = 0 AND last_visit_date BETWEEN ? AND ? AND {where} "
            "ORDER BY last_visit_date DESC LIMIT ?"
        )
        bounds = [int(since * 1e6), int(until * 1e6)]
        database = profile.folder / "places.sqlite"
    else:
        where, params = _like(words, "title", "url")
        sql = (
            f"SELECT title, url, last_visit_time / 1000000.0 - {_CHROMIUM_EPOCH_S}, visit_count FROM urls "  # noqa: S608
            f"WHERE hidden = 0 AND last_visit_time BETWEEN ? AND ? AND {where} "
            "ORDER BY last_visit_time DESC LIMIT ?"
        )
        bounds = [int((since + _CHROMIUM_EPOCH_S) * 1e6), int((until + _CHROMIUM_EPOCH_S) * 1e6)]
        database = profile.folder / "History"
    if not database.is_file():
        return []
    with _copy(database, scratch) as db:
        rows = db.execute(sql, [*bounds, *params, MAX_ROWS]).fetchall()
    return [
        Saved(profile.browser, "history", str(t or ""), str(u), float(w or 0), int(v or 0))
        for t, u, w, v in rows
    ]


def bookmarks(profile: Profile, scratch: Path, words: list[str]) -> list[Saved]:
    """Bookmarks whose name or address has every word."""
    if profile.family == "firefox":
        where, params = _like(words, "b.title", "p.url")
        sql = (
            "SELECT b.title, p.url, b.dateAdded / 1000000.0, p.visit_count FROM moz_bookmarks b "  # noqa: S608
            f"JOIN moz_places p ON p.id = b.fk WHERE b.type = 1 AND {where} "
            "ORDER BY b.dateAdded DESC LIMIT ?"
        )
        with _copy(profile.folder / "places.sqlite", scratch) as db:
            rows = db.execute(sql, [*params, MAX_ROWS]).fetchall()
        return [
            Saved(profile.browser, "bookmark", str(t or ""), str(u), float(w or 0), int(v or 0))
            for t, u, w, v in rows
        ]
    path = profile.folder / "Bookmarks"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[Saved] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict) or len(found) >= MAX_ROWS:
            return
        if node.get("type") == "url":
            title, url = str(node.get("name") or ""), str(node.get("url") or "")
            text = f"{title} {url}".casefold()
            if all(w.casefold() in text for w in words):
                added = (
                    int(node.get("date_added") or 0) / 1e6 - _CHROMIUM_EPOCH_S
                    if node.get("date_added")
                    else 0
                )
                found.append(Saved(profile.browser, "bookmark", title, url, max(0.0, added)))
        for child in node.get("children") or []:
            walk(child)

    for root in (data.get("roots") or {}).values():
        walk(root)
    return found
