"""The installed-application catalog, built from XDG desktop entries.

Search order follows the XDG Base Directory spec: ``$XDG_DATA_HOME/applications`` first, then
each ``$XDG_DATA_DIRS/*/applications``. The first file with a given desktop-file ID wins —
including ``Hidden=true`` files, which is how users "delete" system entries.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sonu.desktop.apps.entry import AppEntry, DesktopEntryError, parse_desktop_entry, visible_on

DEFAULT_DATA_DIRS = "/usr/local/share:/usr/share"


def application_dirs(env: Mapping[str, str]) -> list[Path]:
    home = Path(env.get("HOME") or Path.home())
    data_home = env.get("XDG_DATA_HOME", "")
    first = Path(data_home) if data_home and Path(data_home).is_absolute() else home / ".local" / "share"
    rest = [Path(part) for part in (env.get("XDG_DATA_DIRS") or DEFAULT_DATA_DIRS).split(":") if part]
    ordered: list[Path] = []
    for base in (first, *rest):
        if base.is_absolute() and (base / "applications") not in ordered:
            ordered.append(base / "applications")
    return ordered


def _try_exec_ok(value: str, which: Callable[[str], str | None]) -> bool:
    if not value:
        return True
    path = Path(value)
    if path.is_absolute():
        return path.is_file() and os.access(path, os.X_OK)
    return which(value) is not None


@dataclass(frozen=True, slots=True)
class AppCatalog:
    entries: tuple[AppEntry, ...]
    skipped: int = 0  # malformed files

    @classmethod
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
    ) -> AppCatalog:
        env = os.environ if env is None else env
        desktops = tuple(part for part in env.get("XDG_CURRENT_DESKTOP", "").split(":") if part)
        seen: set[str] = set()
        entries: list[AppEntry] = []
        skipped = 0
        for base in application_dirs(env):
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.desktop")):
                desktop_id = path.relative_to(base).as_posix().replace("/", "-")
                if desktop_id in seen:
                    continue
                seen.add(desktop_id)
                try:
                    entry = parse_desktop_entry(path, desktop_id)
                except (OSError, UnicodeError, DesktopEntryError):
                    skipped += 1
                    continue
                if entry is None or not visible_on(entry, desktops):
                    continue
                if not _try_exec_ok(entry.try_exec, which):
                    continue
                entries.append(entry)
        return cls(tuple(entries), skipped)

    def by_id(self, desktop_id: str) -> AppEntry | None:
        wanted = desktop_id if desktop_id.endswith(".desktop") else f"{desktop_id}.desktop"
        return next((entry for entry in self.entries if entry.desktop_id == wanted), None)

    def by_executable(self, command: str) -> list[AppEntry]:
        name = Path(command).name
        return [entry for entry in self.entries if entry.executable == name and not entry.no_display]
