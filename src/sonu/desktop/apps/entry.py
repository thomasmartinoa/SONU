"""Parsing XDG Desktop Entry files (``*.desktop``), following the freedesktop.org specification.

Only what SONU needs is implemented: the ``[Desktop Entry]`` group, string/list/boolean values,
and the ``Exec`` key's quoting and field codes. Localised keys (``Name[de]=``) are ignored in
favour of the untranslated value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AcceptsArgs = Literal["none", "file", "files", "url", "urls"]

_FIELD_CODES_ACCEPTING: dict[str, AcceptsArgs] = {"%f": "file", "%F": "files", "%u": "url", "%U": "urls"}
# %i, %c, %k expand to icon/name/path; %d %D %n %N %v %m are deprecated. SONU passes none of them.
_DROPPED_FIELD_CODES = {"%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"}
_RESERVED_IN_QUOTES = {'"', "`", "$", "\\"}
_MAX_FILE_BYTES = 256 * 1024


class DesktopEntryError(ValueError):
    """A desktop file is malformed in a way SONU cannot safely interpret."""


@dataclass(frozen=True, slots=True)
class AppEntry:
    desktop_id: str  # e.g. "code-oss.desktop" (path under applications/, "/" replaced by "-")
    path: Path
    name: str
    generic_name: str = ""
    comment: str = ""
    keywords: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()  # Exec with field codes removed
    accepts: AcceptsArgs = "none"
    terminal: bool = False
    startup_wm_class: str = ""
    no_display: bool = False
    try_exec: str = ""
    only_show_in: tuple[str, ...] = ()
    not_show_in: tuple[str, ...] = ()

    @property
    def stem(self) -> str:
        return self.desktop_id.removesuffix(".desktop")

    @property
    def executable(self) -> str:
        return Path(self.argv[0]).name if self.argv else ""


def _unescape_string(value: str) -> str:
    """Desktop-entry string escapes: \\s \\n \\t \\r \\\\ (applied before Exec parsing)."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(nxt, "\\" + nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_list(value: str) -> tuple[str, ...]:
    items = re.split(r"(?<!\\);", value)
    return tuple(item.replace("\\;", ";").strip() for item in items if item.strip())


def _boolean(value: str) -> bool:
    return value.strip().lower() == "true"


def parse_exec(value: str) -> tuple[tuple[str, ...], AcceptsArgs]:
    """Split an (already string-unescaped) ``Exec`` value into argv, removing field codes.

    Returns the argv and which kind of arguments the program accepts (%f/%F/%u/%U).
    """
    args: list[str] = []
    current: list[str] = []
    in_quotes = False
    started = False
    index = 0
    while index < len(value):
        char = value[index]
        if in_quotes:
            if char == "\\" and index + 1 < len(value) and value[index + 1] in _RESERVED_IN_QUOTES:
                current.append(value[index + 1])
                index += 2
                continue
            if char == '"':
                in_quotes = False
            else:
                current.append(char)
        elif char == '"':
            in_quotes = True
            started = True
        elif char in " \t":
            if started:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if in_quotes:
        raise DesktopEntryError("unterminated quote in Exec")
    if started:
        args.append("".join(current))

    accepts: AcceptsArgs = "none"
    cleaned: list[str] = []
    for arg in args:
        if arg in _FIELD_CODES_ACCEPTING:
            accepts = _FIELD_CODES_ACCEPTING[arg]
            continue
        if arg in _DROPPED_FIELD_CODES:
            continue
        # Field codes embedded inside a larger argument: drop them, keep literal %.
        cleaned_arg = re.sub(r"%[fFuUickdDnNvm]", "", arg).replace("%%", "%")
        if cleaned_arg:
            cleaned.append(cleaned_arg)
    if not cleaned:
        raise DesktopEntryError("Exec has no program")
    return tuple(cleaned), accepts


def read_desktop_group(path: Path) -> dict[str, str]:
    """Return the untranslated keys of the ``[Desktop Entry]`` group."""
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise DesktopEntryError("file too large")
    values: dict[str, str] = {}
    in_group = False
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_group:  # only the first [Desktop Entry] group matters
                break
            in_group = line == "[Desktop Entry]"
            continue
        if not in_group or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if "[" in key:  # localised variant, e.g. Name[de]
            continue
        values.setdefault(key, value.strip())
    return values


def parse_desktop_entry(path: Path, desktop_id: str) -> AppEntry | None:
    """Parse an application entry. Returns None for non-applications and hidden (deleted) entries."""
    group = read_desktop_group(path)
    if group.get("Type") != "Application" or _boolean(group.get("Hidden", "false")):
        return None
    name = _unescape_string(group.get("Name", "")).strip()
    exec_value = group.get("Exec", "")
    if not name or not exec_value:
        return None
    argv, accepts = parse_exec(_unescape_string(exec_value))
    return AppEntry(
        desktop_id=desktop_id,
        path=path,
        name=name,
        generic_name=_unescape_string(group.get("GenericName", "")),
        comment=_unescape_string(group.get("Comment", "")),
        keywords=_split_list(_unescape_string(group.get("Keywords", ""))),
        categories=_split_list(group.get("Categories", "")),
        argv=argv,
        accepts=accepts,
        terminal=_boolean(group.get("Terminal", "false")),
        startup_wm_class=_unescape_string(group.get("StartupWMClass", "")).strip(),
        no_display=_boolean(group.get("NoDisplay", "false")),
        try_exec=_unescape_string(group.get("TryExec", "")).strip(),
        only_show_in=_split_list(group.get("OnlyShowIn", "")),
        not_show_in=_split_list(group.get("NotShowIn", "")),
    )


def visible_on(entry: AppEntry, current_desktops: tuple[str, ...]) -> bool:
    """Apply OnlyShowIn / NotShowIn against XDG_CURRENT_DESKTOP (e.g. ``("Hyprland",)``)."""
    desktops = {desktop.lower() for desktop in current_desktops}
    if entry.only_show_in and not desktops & {item.lower() for item in entry.only_show_in}:
        return False
    return not desktops & {item.lower() for item in entry.not_show_in}
