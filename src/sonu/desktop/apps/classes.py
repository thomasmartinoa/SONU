"""Deciding whether a window belongs to an application.

Window classes are not standardised: Code - OSS reports ``code-oss``, VSCodium ``VSCodium``,
Zen ``zen`` (its executable is ``zen-browser``). SONU compares a window's class against several
identifiers from the desktop entry — ``StartupWMClass``, the desktop-file ID, the executable and
the name — exactly, or as a prefix at a word boundary (``zen`` ↔ ``zen-browser``). Classes SONU
has observed for an app before ("learned") are accepted too.

A web app Chromium opens (``--app=URL``) gets a class made from the link: ``chrome-<host>__<path>-<profile>``,
where each ``/`` of the path became ``_``. Found on the laptop: the Gmail web app's entry said
``chrome-mail.google.com__mail-Default`` but its window was ``chrome-mail.google.com__mail_-Default`` (the
link's last ``/``), so SONU couldn't confirm Gmail had opened. Those trailing ``_`` don't count.

This is only ever used on *new* windows (not present before a launch) or to find existing
windows of a named app, which keeps false positives rare.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sonu.desktop.apps.entry import AppEntry

_BOUNDARY = "-_. "
_WEB_APP = re.compile(r"^(?:chrome|chromium|brave|msedge|vivaldi)-.+__")
_PATH_END = re.compile(r"_+(?=-[^-]+$)")  # "…__mail_-Default" → "…__mail-Default"


def _same(value: str) -> str:
    value = value.lower().strip()
    return _PATH_END.sub("", value) if _WEB_APP.match(value) else value


def expected_classes(entry: AppEntry) -> tuple[str, ...]:
    stem = entry.stem
    raw = (entry.startup_wm_class, stem, stem.rsplit(".", 1)[-1], entry.executable, entry.name)
    seen: list[str] = []
    for value in raw:
        lowered = value.lower().strip()
        if lowered and lowered not in seen:
            seen.append(lowered)
    return tuple(seen)


def _boundary_prefix(short: str, long: str) -> bool:
    return (
        len(short) >= 3
        and len(long) > len(short)
        and long.startswith(short)
        and long[len(short)] in _BOUNDARY
    )


def window_matches(entry: AppEntry, classes: Iterable[str], learned: Iterable[str] = ()) -> bool:
    candidates = {_same(value) for value in (*expected_classes(entry), *learned)}
    for window_class in classes:
        value = _same(window_class)
        if not value:
            continue
        if value in candidates:
            return True
        if any(_boundary_prefix(value, c) or _boundary_prefix(c, value) for c in candidates):
            return True
    return False
