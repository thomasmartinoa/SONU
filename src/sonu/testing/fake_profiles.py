"""Browser profiles with a made-up history and bookmarks (D73), laid out like the real ones: Zen/Firefox
``places.sqlite`` (the tables and columns SONU reads, in WAL mode), Chromium ``History`` + ``Bookmarks``."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_CHROMIUM_EPOCH_S = 11644473600

# (title, url, minutes ago)
Visit = tuple[str, str, float]


def firefox_profile(
    home: Path, visits: list[Visit], marks: list[Visit] | None = None, browser: str = "zen"
) -> Path:
    root = home / (".config/zen" if browser == "zen" else ".mozilla/firefox")
    folder = root / "abcd1234.Default (release)"
    folder.mkdir(parents=True, exist_ok=True)
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=Default (release)\nIsRelative=1\nPath=abcd1234.Default (release)\nDefault=1\n"
    )
    db = sqlite3.connect(folder / "places.sqlite")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute(
        "CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, "
        "hidden INTEGER DEFAULT 0, last_visit_date INTEGER)"
    )
    db.execute(
        "CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER, title TEXT, "
        "dateAdded INTEGER)"
    )
    now = time.time()
    for title, url, minutes in visits:
        db.execute(
            "INSERT INTO moz_places (url, title, visit_count, last_visit_date) VALUES (?, ?, 1, ?)",
            (url, title, int((now - minutes * 60) * 1e6)),
        )
    for title, url, minutes in marks or []:
        cursor = db.execute(
            "INSERT INTO moz_places (url, title, visit_count, last_visit_date) VALUES (?, ?, 0, NULL)",
            (url, title),
        )
        db.execute(
            "INSERT INTO moz_bookmarks (type, fk, title, dateAdded) VALUES (1, ?, ?, ?)",
            (cursor.lastrowid, title, int((now - minutes * 60) * 1e6)),
        )
    db.commit()  # left in the -wal file, like a running browser's latest visits
    db.close()
    return folder


def chromium_profile(home: Path, visits: list[Visit], marks: list[Visit] | None = None) -> Path:
    folder = home / ".config" / "chromium" / "Default"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "History")
    db.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, "
        "typed_count INTEGER DEFAULT 0, last_visit_time INTEGER, hidden INTEGER DEFAULT 0)"
    )
    now = time.time()
    for title, url, minutes in visits:
        db.execute(
            "INSERT INTO urls (url, title, visit_count, last_visit_time) VALUES (?, ?, 1, ?)",
            (url, title, int((now - minutes * 60 + _CHROMIUM_EPOCH_S) * 1e6)),
        )
    db.commit()
    db.close()
    if marks:
        children = [
            {
                "type": "url",
                "name": title,
                "url": url,
                "date_added": str(int((now - minutes * 60 + _CHROMIUM_EPOCH_S) * 1e6)),
            }
            for title, url, minutes in marks
        ]
        roots = {"bookmark_bar": {"type": "folder", "name": "Bookmarks bar", "children": children}}
        (folder / "Bookmarks").write_text(json.dumps({"roots": roots, "version": 1}))
    return folder
