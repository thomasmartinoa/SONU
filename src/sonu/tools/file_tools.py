"""Opening folders and files: "open my downloads", "open dev_works", "open my last download".

* Folders are found by SONU, never by the model: the XDG user folders (``user-dirs.dirs``), "home",
  a path the user typed (inside their home), or a folder in the home tree with that name. Several
  matches → the user picks. Folders open in the rice's file manager (``fileManager`` in
  ``binds.lua``), not ``xdg-open``: on the owner's laptop the default for folders is a terminal.
* "The newest file" is the newest regular file in the folder (partial downloads and hidden files
  skipped). It opens with the app the desktop has set for its type (``xdg-mime``). Programs,
  scripts and launchers are never opened — a download could be anything; types other than
  documents, pictures, sound and video ask first.
* Every open is confirmed by the app's window showing the folder or file name.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import cast

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
        folders = find_folders(a.place, home, ctx.system.screenshot_dir())
        if choice is not None:
            folders = [folder for folder in folders if str(folder) == choice]
        if not folders:
            name = " ".join(w for w in normalize(_NEWEST.sub(" ", a.place)).split() if w not in _FILLERS)
            return ToolOutcome(
                StepStatus.FAILED, f"There's no folder called '{name or a.place}' in your home"
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
        return await self._prepare_file(folder, ctx)

    @staticmethod
    async def _prepare_file(folder: Path, ctx: ToolContext) -> Prepared | ToolOutcome:
        where = _home_relative(folder, ctx.system.home)
        file = newest_file(folder)
        if file is None:
            return ToolOutcome(StepStatus.FAILED, f"There are no files in {where}")
        if not ctx.system.available("xdg-mime"):
            return ToolOutcome(
                StepStatus.FAILED, "xdg-mime isn't installed (install: sudo pacman -S xdg-utils)"
            )
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
                f"The newest file in {where}, {label}, is {kind} — SONU doesn't open those (it could run it)",
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
            f"Open {label} (the newest file in {where}) with {entry.name}",
            _OpenPlan(entry, (*argv, str(file)), file, label),
            risk=None if safe else Risk.MEDIUM,  # an archive, an ISO, a torrent…: ask first
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        plan = cast(_OpenPlan, prepared.payload)
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

        window, snapshot = await ctx.desktop.wait_for(
            showing_it, timeout_s=ctx.settings.apps.launch_timeout_s
        )
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


FILE_TOOLS: tuple[Tool, ...] = (OpenFiles(),)
