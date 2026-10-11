"""Folders and files: "open my downloads", "open dev_works", "open my last download", "open my resume",
"where is the invoice pdf?" (D74), "what files did I open recently?" (D74).

* Folders are found by SONU, never by the model: the XDG user folders (``user-dirs.dirs``), "home",
  a path the user typed (inside their home), or a folder in the home tree with that name. Several
  matches → the user picks. Folders open in the rice's file manager (``fileManager`` in
  ``binds.lua``), not ``xdg-open``: on the owner's laptop the default for folders is a terminal.
* "The newest file" is the newest regular file in the folder (partial downloads and hidden files
  skipped). It opens with the app the desktop has set for its type (``xdg-mime``). Programs,
  scripts and launchers are never opened — a download could be anything; types other than
  documents, pictures, sound and video ask first.
* A file by its name (D74): when no folder has that name, the files whose name has every one of the
  user's words ("resume", "invoice pdf", "notes.txt"), newest first; several → the user picks. Hidden
  folders, dependencies and build output aren't searched. The same rules then apply as for the newest file.
* Every open is confirmed by the app's window showing the folder or file name.
* File names are the user's: lists are private results, never given to a model.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.system.commands import CommandError
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.desktop_tools import failed_dispatch, resolve_app
from sonu.tools.journal_tools import When, period
from sonu.tools.web_tools import compact
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

# XDG user folder keys, by the words people use for them.
_USER_DIRS: dict[str, str] = {
    "desktop": "XDG_DESKTOP_DIR",
    "download": "XDG_DOWNLOAD_DIR",
    "downloads": "XDG_DOWNLOAD_DIR",
    "document": "XDG_DOCUMENTS_DIR",
    "documents": "XDG_DOCUMENTS_DIR",
    "docs": "XDG_DOCUMENTS_DIR",
    "music": "XDG_MUSIC_DIR",
    "picture": "XDG_PICTURES_DIR",
    "pictures": "XDG_PICTURES_DIR",
    "photos": "XDG_PICTURES_DIR",
    "video": "XDG_VIDEOS_DIR",
    "videos": "XDG_VIDEOS_DIR",
    "templates": "XDG_TEMPLATES_DIR",
    "public": "XDG_PUBLICSHARE_DIR",
}
_XDG_FALLBACKS = {
    "XDG_DESKTOP_DIR": "Desktop",
    "XDG_DOWNLOAD_DIR": "Downloads",
    "XDG_DOCUMENTS_DIR": "Documents",
    "XDG_MUSIC_DIR": "Music",
    "XDG_PICTURES_DIR": "Pictures",
    "XDG_VIDEOS_DIR": "Videos",
    "XDG_TEMPLATES_DIR": "Templates",
    "XDG_PUBLICSHARE_DIR": "Public",
}
_USER_DIR_LINE = re.compile(r'^(XDG_[A-Z]+_DIR)="(.*)"\s*$')
_FILLERS = {"my", "the", "folder", "folders", "directory", "dir", "open", "in", "files", "file"}
# "my last download", "the newest file in documents": the newest file, not the folder. Found in an
# eval: the model put "my last download" in place and left newest out.
_NEWEST = re.compile(r"\b(last|latest|newest|most recent|recent)\b", re.IGNORECASE)
# "open my last download": found in the evals (v31) — the model dropped "last" from the place.
_NEWEST_FILE = re.compile(
    r"\b(?:last|latest|newest|most recent)\s+(?:download|file|screenshot|photo|picture|video|document)s?\b",
    re.IGNORECASE,
)
# Not searched: they're huge and nobody means them ("open build" means their own folder).
_SKIP = {"node_modules", "__pycache__", "site-packages", "venv", "target", "snap"}
SEARCH_DEPTH = 3
MAX_DIRS_SCANNED = 20_000
MAX_CHOICES = 6
_PARTIAL = (".part", ".crdownload", ".download", ".tmp", ".partial", ".opdownload")

# Never opened: programs, scripts and launchers (opening one can run it).
_NEVER_OPEN = {
    "application/x-executable",
    "application/x-pie-executable",
    "application/x-sharedlib",
    "application/x-shellscript",
    "application/x-desktop",
    "application/vnd.appimage",
    "application/x-msdownload",
    "application/x-ms-dos-executable",
    "application/x-msi",
    "application/x-ms-shortcut",
    "application/x-java-archive",
    "application/x-perl",
    "application/x-ruby",
}
# Opened without asking: documents, pictures, sound and video.
_SAFE_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/vnd.oasis.opendocument.",
    "application/vnd.openxmlformats-",
)
_SAFE_TYPES = {"text/plain", "text/markdown", "application/pdf", "application/epub+zip", "application/msword"}


def user_dirs(home: Path) -> dict[str, Path]:
    """XDG user folder key → path (``~/.config/user-dirs.dirs``, else the usual English names)."""
    found = {key: home / name for key, name in _XDG_FALLBACKS.items()}
    try:
        lines = (home / ".config" / "user-dirs.dirs").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return found
    for line in lines:
        if (match := _USER_DIR_LINE.match(line.strip())) and match.group(1) in found:
            value = match.group(2).replace("$HOME", str(home))
            path = Path(value)
            if path.is_absolute() and path != home:
                found[match.group(1)] = path
    return found


def _inside(path: Path, home: Path) -> bool:
    try:
        path.resolve().relative_to(home.resolve())
    except ValueError:
        return False
    return True


def _search(home: Path, key: str) -> list[Path]:
    """Folders in the home tree named ``key`` (exact first, then close spellings), shallowest first."""
    exact: list[tuple[int, Path]] = []
    close: list[tuple[int, Path]] = []
    queue: deque[tuple[Path, int]] = deque([(home, 0)])
    scanned = 0
    while queue and scanned < MAX_DIRS_SCANNED:
        folder, depth = queue.popleft()
        try:
            children = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for child in children:
            if child.name.startswith(".") or child.name in _SKIP:
                continue
            try:
                if not child.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            scanned += 1
            name = compact(child.name)
            if name == key or name.removesuffix("s") == key.removesuffix("s"):  # screenshot(s)
                exact.append((depth, Path(child.path)))
            elif len(key) >= 4 and SequenceMatcher(None, name, key).ratio() >= 0.85:
                close.append((depth, Path(child.path)))
            if depth + 1 < SEARCH_DEPTH:
                queue.append((Path(child.path), depth + 1))
    return [path for _, path in sorted(exact or close, key=lambda item: (item[0], str(item[1])))]


def find_folders(place: str, home: Path, screenshots: Path | None = None) -> list[Path]:
    """The folder(s) the user's words name — decided here, never by the model."""
    text = place.strip()
    if text.startswith(("~/", "/")) or text == "~":
        path = Path(text.replace("~", str(home), 1)).expanduser()
        return [path] if path.is_dir() and _inside(path, home) else []
    if "/" in text.strip("/"):  # "dev_works/SONU": a path from the home folder
        path = home / text
        return [path] if path.is_dir() and _inside(path, home) else []
    words = [word for word in normalize(_NEWEST.sub(" ", text)).split() if word not in _FILLERS]
    if not words or words == ["home"]:
        return [home]
    if words[0] in ("screenshot", "screenshots") and len(words) == 1 and screenshots and screenshots.is_dir():
        return [screenshots]  # where SONU and the rice's keys save them
    if len(words) == 1 and words[0] in _USER_DIRS:
        path = user_dirs(home)[_USER_DIRS[words[0]]]
        return [path] if path.is_dir() else []
    return _search(home, compact(" ".join(words)))


def newest_file(folder: Path) -> Path | None:
    """The newest regular file in ``folder`` (not hidden, not a download still in progress)."""
    newest: tuple[float, Path] | None = None
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return None
    for entry in entries:
        if entry.name.startswith(".") or entry.name.endswith(_PARTIAL):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, Path(entry.path))
    return newest[1] if newest else None


# "my vacation photos", "the invoice pdf": a kind of file, by its usual extensions.
_KINDS: dict[str, frozenset[str]] = {
    **dict.fromkeys(("pdf", "pdfs"), frozenset({".pdf"})),
    **dict.fromkeys(
        ("photo", "photos", "picture", "pictures", "image", "images", "pic", "pics"),
        frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".avif"}),
    ),
    **dict.fromkeys(
        ("video", "videos", "movie", "movies"), frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi"})
    ),
    **dict.fromkeys(
        ("song", "songs", "music", "audio", "track"),
        frozenset({".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav"}),
    ),
    **dict.fromkeys(
        ("document", "documents", "doc", "docs"),
        frozenset({".pdf", ".odt", ".docx", ".doc", ".txt", ".md", ".rtf", ".epub"}),
    ),
    **dict.fromkeys(("spreadsheet", "spreadsheets", "sheet"), frozenset({".ods", ".xlsx", ".xls", ".csv"})),
    **dict.fromkeys(("presentation", "slides", "deck"), frozenset({".odp", ".pptx", ".ppt"})),
}
_NAME_FILLERS = _FILLERS | {
    "a",
    "an",
    "called",
    "named",
    "where",
    "is",
    "are",
    "find",
    "show",
    "me",
    "of",
    "for",
    "from",
    "that",
    "this",
    "with",
    "and",
    "put",
    "saved",
    "i",
}
MAX_FILES_SCANNED = 300_000
MAX_FOUND = 50


def file_words(name: str) -> tuple[list[str], frozenset[str] | None]:
    """The words a file's name must have, and the extensions a kind word allows ("invoice pdf")."""
    words: list[str] = []
    kinds: set[str] = set()
    for word in normalize(name.replace(".", " ")).split():
        if word in _NAME_FILLERS:
            continue
        if word in _KINDS:
            kinds |= _KINDS[word]
        else:
            words.append(word)
    return words, frozenset(kinds) or None


def files_named(folder: Path, words: list[str], kinds: frozenset[str] | None) -> list[Path]:
    """Files under ``folder`` whose name has every word (a kind: one of its extensions), newest first.
    Not searched: hidden folders, dependencies and build output."""
    if not words and not kinds:
        return []
    keys = [compact(w) for w in words if compact(w)]
    found: list[tuple[float, Path]] = []
    stack = [folder]
    scanned = 0
    while stack and scanned < MAX_FILES_SCANNED:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if entry.name.startswith(".") or entry.name in _SKIP:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False) or entry.name.endswith(_PARTIAL):
                    continue
                name = compact(entry.name)
                if not all(key in name for key in keys):
                    continue
                if kinds and Path(entry.name).suffix.lower() not in kinds:
                    continue
                found.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
            except OSError:
                continue
    found.sort(key=lambda item: -item[0])
    # The file the words name exactly ("notes" → notes.txt) before the ones that only contain them.
    whole = compact(" ".join(words))
    exact = [p for _, p in found if compact(p.stem) == whole]
    return (exact + [p for _, p in found if p not in exact])[:MAX_FOUND]


def _home_relative(path: Path, home: Path) -> str:
    try:
        rest = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if str(rest) == "." else f"~/{rest}"


@dataclass(frozen=True, slots=True)
class _OpenPlan:
    entry: AppEntry
    argv: tuple[str, ...]
    target: Path
    label: str  # what the user sees: "~/Downloads" or "'report.pdf'"


class OpenArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place: str = Field(
        min_length=1,
        max_length=160,
        description="the folder as the user named it: 'downloads', 'dev_works', '~/Pictures/screenshot'",
    )
    newest: bool = Field(
        default=False,
        description="true for 'my last download' / 'the newest file in X': open that file, not the folder",
    )


class OpenFiles(Tool):
    name = "files.open"
    description = (
        "Open a folder in the file manager ('open my downloads', 'open dev_works'), or the newest file "
        "in a folder with its app ('open my last download')."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH, Effect.FILE_READ})
    Args = OpenArgs
    grounded = ("place",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(OpenArgs, args)
        home = ctx.system.home
        if choice is not None and (picked := _picked_file(choice, home)) is not None:
            return await _prepare_open(picked, ctx)  # the file the user picked
        folders = find_folders(a.place, home, ctx.system.screenshot_dir())
        if choice is not None:
            folders = [folder for folder in folders if str(folder) == choice]
        if not folders:
            # No folder by that name: a file (D74) — "open my resume", "open notes.txt", "dev_works/a.md".
            files = await asyncio.to_thread(_files_for, a.place, home)
            if len(files) == 1:
                return await _prepare_open(files[0], ctx)
            if len(files) > 1:
                shown = files[:MAX_CHOICES]
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT,
                    f"{len(files)} files match '{a.place}'",
                    question=f"Which '{a.place}'?",
                    choices=Choices(
                        f"Which '{a.place}'?",
                        tuple(Choice(_file_label(f, home), str(f)) for f in shown),
                    ),
                    private=True,
                )
            name = " ".join(w for w in normalize(_NEWEST.sub(" ", a.place)).split() if w not in _FILLERS)
            return ToolOutcome(
                StepStatus.FAILED, f"There's no folder or file called '{name or a.place}' in your home"
            )
        if len(folders) > 1:
            shown = folders[:MAX_CHOICES]
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"several folders are called '{a.place}'",
                question=f"Which one: {', '.join(_home_relative(f, home) for f in shown)}?",
                choices=Choices(
                    f"Which '{a.place}' folder?",
                    tuple(Choice(_home_relative(folder, home), str(folder)) for folder in shown),
                ),
            )
        folder = folders[0]
        if not (a.newest or _NEWEST.search(a.place) or _NEWEST_FILE.search(ctx.request_text)):
            manager = resolve_app("file manager", ctx)
            if isinstance(manager, ToolOutcome):
                return manager
            argv = ctx.launch_argv(manager)
            if argv is None:
                return ToolOutcome(StepStatus.FAILED, f"{manager.name} can't be started")
            label = _home_relative(folder, home)
            return Prepared(
                f"Open {label} in {manager.name}", _OpenPlan(manager, (*argv, str(folder)), folder, label)
            )
        where = _home_relative(folder, home)
        file = newest_file(folder)
        if file is None:
            return ToolOutcome(StepStatus.FAILED, f"There are no files in {where}")
        return await _prepare_open(file, ctx, f" (the newest file in {where})")

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        return await _open(cast(_OpenPlan, prepared.payload), ctx)


def _files_for(place: str, home: Path) -> list[Path]:
    """The file(s) the user's words name, when no folder does."""
    text = place.strip()
    if text.startswith(("~/", "/")) or "/" in text.strip("/"):
        path = Path(text.replace("~", str(home), 1)) if text.startswith("~") else home / text
        if text.startswith("/"):
            path = Path(text)
        return [path] if path.is_file() and _inside(path, home) else []
    words, kinds = file_words(text)
    return files_named(home, words, kinds)


def _picked_file(choice: str, home: Path) -> Path | None:
    path = Path(choice)
    return path if path.is_file() and _inside(path, home) else None


def _file_label(path: Path, home: Path) -> str:
    return f"{path.name} — {_home_relative(path.parent, home)}"


async def _prepare_open(file: Path, ctx: ToolContext, why: str = "") -> Prepared | ToolOutcome:
    """Open one file with the app set for its type: never a program or script; unusual types ask."""
    if not ctx.system.available("xdg-mime"):
        return ToolOutcome(StepStatus.FAILED, "xdg-mime isn't installed (install: sudo pacman -S xdg-utils)")
    try:
        mime = await ctx.system.mime_type(file)
        app_id = await ctx.system.default_app(mime) if mime else None
    except CommandError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't tell what kind of file '{file.name}' is: {exc}")
    label = f"'{file.name}'"
    executable = os.access(file, os.X_OK)
    if mime is None or mime in _NEVER_OPEN or executable:
        kind = "a program or script" if executable or mime in _NEVER_OPEN else "an unknown kind of file"
        return ToolOutcome(
            StepStatus.FAILED,
            f"{label}{why} is {kind} — SONU doesn't open those (it could run it)",
        )
    entry = ctx.resolver.catalog.by_id(app_id) if app_id else None
    if entry is None:
        return ToolOutcome(StepStatus.FAILED, f"No app is set to open {mime} files like {label}")
    if entry.accepts == "none":
        return ToolOutcome(StepStatus.FAILED, f"{entry.name} can't be given a file to open")
    argv = ctx.launch_argv(entry)
    if argv is None:
        return ToolOutcome(StepStatus.FAILED, f"{entry.name} can't be started")
    safe = mime in _SAFE_TYPES or mime.startswith(_SAFE_PREFIXES)
    return Prepared(
        f"Open {label}{why} with {entry.name}",
        _OpenPlan(entry, (*argv, str(file)), file, label),
        risk=None if safe else Risk.MEDIUM,  # an archive, an ISO, a torrent…: ask first
    )


async def _open(plan: _OpenPlan, ctx: ToolContext) -> ToolOutcome:
    before = await ctx.desktop.snapshot()
    titles = {window.address: window.title for window in before.windows}
    learned = ctx.learned(plan.entry)
    marker = compact(plan.target.stem if plan.target.is_file() else plan.target.name)[:24]
    try:
        await ctx.desktop.launch(plan.argv)
    except HyprlandError as exc:
        return failed_dispatch(f"start {plan.entry.name}", exc)

    def shown(snapshot: WorldSnapshot) -> list[Window]:
        return [
            window
            for window in snapshot.windows
            if window_matches(plan.entry, (window.window_class, window.initial_class), learned)
            and titles.get(window.address) != window.title
        ]

    def showing_it(snapshot: WorldSnapshot) -> Window | None:
        return next((w for w in shown(snapshot) if marker and marker in compact(w.title)), None)

    window, snapshot = await ctx.desktop.wait_for(showing_it, timeout_s=ctx.settings.apps.launch_timeout_s)
    if window is not None:
        check = Check(f"{plan.entry.name} shows {plan.label}", True, {"class": window.window_class})
        return ToolOutcome(
            StepStatus.DONE, f"Opened {plan.label} in {plan.entry.name}", (check,), private=True
        )
    if shown(snapshot):
        return ToolOutcome(
            StepStatus.UNVERIFIED,
            f"{plan.entry.name} opened, but its window doesn't show {plan.label}, so I can't confirm it",
            private=True,
        )
    return ToolOutcome(
        StepStatus.FAILED, f"{plan.entry.name} didn't open a window for {plan.label}", private=True
    )


# ---------------------------------------------------------------------------- files.find
class FindArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=120, description="the user's words for the file: 'invoice pdf', 'notes.txt'"
    )
    place: str | None = Field(default=None, max_length=160, description="a folder the user named, if any")


class FindFiles(Tool):
    name = "files.find"
    description = "Find files by the user's words for their name ('where is my invoice pdf?'); read-only."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.FILE_READ})
    Args = FindArgs
    grounded = ("name", "place")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(FindArgs, args)
        home = ctx.system.home
        folder = home
        if a.place:
            folders = find_folders(a.place, home, ctx.system.screenshot_dir())
            if choice is not None:
                folders = [f for f in folders if str(f) == choice]
            if not folders:
                return ToolOutcome(StepStatus.FAILED, f"There's no folder called '{a.place}' in your home")
            if len(folders) > 1:
                shown = folders[:MAX_CHOICES]
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT,
                    f"several folders are called '{a.place}'",
                    question=f"Which '{a.place}' folder?",
                    choices=Choices(
                        f"Which '{a.place}' folder?",
                        tuple(Choice(_home_relative(f, home), str(f)) for f in shown),
                    ),
                )
            folder = folders[0]
        words, kinds = file_words(a.name)
        if not words and not kinds:
            return ToolOutcome(StepStatus.FAILED, "Which file? Say a word from its name")
        where = f" in {_home_relative(folder, home)}" if folder != home else ""
        return Prepared(f"Look for files matching “{a.name}”{where}", (folder, words, kinds, a.name))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        folder, words, kinds, said = cast(
            "tuple[Path, list[str], frozenset[str] | None, str]", prepared.payload
        )
        found = await asyncio.to_thread(files_named, folder, words, kinds)
        checked = (Check("the folders were searched", True),)
        if not found:
            return ToolOutcome(StepStatus.DONE, f"No file matches “{said}”", checked)
        home = ctx.system.home
        listed = "; ".join(
            f"{_file_label(f, home)} ({_when(f.stat().st_mtime, ctx)})" for f in found[:MAX_LISTED]
        )
        more = f"; and {len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
        count = "1 file matches" if len(found) == 1 else f"{len(found)} files match"
        # The user's file names: shown to them, never given to a model or the audit log.
        return ToolOutcome(StepStatus.DONE, f"{count} “{said}”: {listed}{more}", checked, private=True)


MAX_LISTED = 8


def _when(stamp: float, ctx: ToolContext) -> str:
    at, now = datetime.fromtimestamp(stamp), ctx.now()
    days = (now.date() - at.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{at:%a %d %b}"
    return f"{day} {at:%H:%M}"


# ---------------------------------------------------------------------------- files.recent
@dataclass(frozen=True, slots=True)
class Recent:
    path: Path
    when: float
    app: str


def recent_files(home: Path) -> list[Recent]:
    """The files apps say were opened recently (``~/.local/share/recently-used.xbel``), newest first,
    only ones that still exist."""
    try:
        root = ElementTree.parse(home / ".local" / "share" / "recently-used.xbel").getroot()  # noqa: S314
    except (OSError, ElementTree.ParseError):
        return []
    found: dict[Path, Recent] = {}
    for mark in root.iter("bookmark"):
        href = mark.get("href", "")
        if not href.startswith("file://"):
            continue
        path = Path(unquote(urlsplit(href).path))
        stamps = [mark.get(k, "") for k in ("modified", "visited")]
        app, app_stamp = "", ""
        for used in mark.iter(f"{{{_BOOKMARK_NS}}}application"):
            if used.get("modified", "") >= app_stamp:
                app, app_stamp = used.get("name", ""), used.get("modified", "")
        when = max((_stamp(t) for t in [*stamps, app_stamp]), default=0.0)
        if path.is_file() and (path not in found or found[path].when < when):
            found[path] = Recent(path, when, app)
    return sorted(found.values(), key=lambda r: -r.when)


_BOOKMARK_NS = "http://www.freedesktop.org/standards/desktop-bookmarks"


def _stamp(text: str) -> float:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class RecentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, max_length=120, description="the user's words for the file, if any"
    )
    when: When | None = Field(default=None, description="only if the user said a time ('yesterday')")


class RecentFiles(Tool):
    name = "files.recent"
    description = (
        "The files the user opened recently ('what files did I open recently / yesterday?'); read-only."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.FILE_READ})
    Args = RecentArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(RecentArgs, args)
        return Prepared("List the files you opened recently", a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(RecentArgs, prepared.payload)
        home = ctx.system.home
        found = await asyncio.to_thread(recent_files, home)
        words, kinds = file_words(a.name or "")
        keys = [compact(w) for w in words]
        found = [
            r
            for r in found
            if all(k in compact(r.path.name) for k in keys) and (not kinds or r.path.suffix.lower() in kinds)
        ]
        said = ""
        if a.when:
            start, end, said = period(a.when, ctx.now())
            found = [r for r in found if start.timestamp() <= r.when < end.timestamp()]
            said = f" {said}"
        checked = (Check("the list of recent files was read", True),)
        if not found:
            return ToolOutcome(StepStatus.DONE, f"No recent files{said}", checked)
        listed = "; ".join(
            f"{_file_label(r.path, home)} ({_when(r.when, ctx)}{', ' + r.app if r.app else ''})"
            for r in found[:MAX_LISTED]
        )
        more = f"; and {len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
        return ToolOutcome(StepStatus.DONE, f"Files you opened{said}: {listed}{more}", checked, private=True)


FILE_TOOLS: tuple[Tool, ...] = (OpenFiles(), FindFiles(), RecentFiles())
