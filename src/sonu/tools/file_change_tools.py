"""Moving, renaming and deleting files — to the trash only — and getting them back (D75): "move my invoice to
documents", "rename notes.txt to ideas", "delete my last download", "restore notes.txt".

* What's changed is found by SONU from the user's words, like opening a file (``file_tools.py``): a path
  they typed, the newest file ("my last download"), a folder or a file with that name; several → they pick.
* Only inside the home folder, never the home folder or its user folders themselves (Downloads, Documents…),
  never hidden files or folders (settings, keys, a project's ``.git``).
* Nothing is ever overwritten, and nothing is deleted: "delete" moves it to the desktop's trash with
  ``gio trash``, which the user (or "restore …") can undo. Every change asks first and is checked after:
  the file is where it should be, and gone from where it was.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import cast
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.resolver import normalize
from sonu.system.commands import CommandError, ProgramMissing
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.file_tools import (
    _NEWEST,
    MAX_CHOICES,
    file_words,
    files_named,
    find_folders,
    home_relative,
    inside_home,
    newest_file,
    user_dirs,
)
from sonu.tools.grounding import ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

SOURCE = "src:"
TARGET = "dst:"


def _label(path: Path, home: Path) -> str:
    kind = "folder" if path.is_dir() else "file"
    return f"the {kind} '{path.name}' ({home_relative(path.parent, home)})"


def protected(path: Path, home: Path) -> str | None:
    """Why SONU won't move, rename or trash ``path`` (None: it may)."""
    if not inside_home(path, home):
        return "it isn't in your home folder"
    resolved, base = path.resolve(), home.resolve()
    if resolved == base:
        return "it's your home folder"
    if resolved in {p.resolve() for p in user_dirs(home).values()}:
        return f"{path.name} is one of your main folders"
    if any(part.startswith(".") for part in resolved.relative_to(base).parts):
        return "hidden files and folders (settings, keys) are left alone"
    return None


def find_sources(words: str, request: str, home: Path, screenshots: Path | None) -> list[Path]:
    """The file(s) or folder(s) the user's words name: a typed path, the newest file in a folder, folders and
    files with that name."""
    text = words.strip()
    if text.startswith(("~/", "/")) or "/" in text.strip("/"):
        path = Path(text.replace("~", str(home), 1)) if text.startswith("~") else home / text
        path = Path(text) if text.startswith("/") else path
        return [path] if path.exists() else []
    if _NEWEST.search(text) or _NEWEST.search(request):
        folders = find_folders(text, home, screenshots)
        newest = newest_file(folders[0]) if len(folders) == 1 else None
        return [newest] if newest else []
    found = [f for f in find_folders(text, home, screenshots) if f != home]
    names, kinds = file_words(text)
    found += [f for f in files_named(home, names, kinds) if f not in found]
    return found


def _ask(what: str, paths: list[Path], home: Path, prefix: str) -> ToolOutcome:
    question = f"Which {what}?"
    options = tuple(Choice(f"{p.name} — {home_relative(p.parent, home)}", prefix + str(p)) for p in paths)
    return ToolOutcome(
        StepStatus.NEEDS_INPUT,
        question,
        question=question,
        choices=Choices(question, options[:MAX_CHOICES]),
        private=True,
    )


def _own_words(ctx: ToolContext, *values: str | None) -> ToolOutcome | None:
    for value in values:
        if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
            return ToolOutcome(StepStatus.FAILED, f"That must be your own words ({', '.join(missing)} isn't)")
    return None


async def _source(ctx: ToolContext, words: str, choice: str | None) -> Path | ToolOutcome:
    home = ctx.system.home
    if choice and choice.startswith(SOURCE):
        sources = [Path(choice.removeprefix(SOURCE))]
    else:
        sources = await asyncio.to_thread(
            find_sources, words, ctx.request_text, home, ctx.system.screenshot_dir()
        )
    if not sources:
        return ToolOutcome(StepStatus.FAILED, f"There's no file or folder called '{words}' in your home")
    if len(sources) > 1:
        return _ask(f"'{words}'", sources, home, SOURCE)
    source = sources[0]
    if not source.exists():
        return ToolOutcome(StepStatus.FAILED, f"'{source.name}' isn't there any more")
    if why := protected(source, home):
        return ToolOutcome(StepStatus.FAILED, f"SONU won't change '{source.name}': {why}")
    return source


def _moved(source: Path, target: Path) -> tuple[Check, Check]:
    return (
        Check("it's in its new place", target.exists()),
        Check("it's gone from where it was", not source.exists()),
    )


# ---------------------------------------------------------------------------- files.move
class MoveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="the user's words for the file or folder")
    to: str = Field(min_length=1, max_length=160, description="the user's words for the folder to move it to")


class MoveFile(Tool):
    name = "files.move"
    description = "Move a file or folder into another folder ('move my invoice to documents'); asks first."
    risk = Risk.MEDIUM
    effects = frozenset({Effect.FILE_WRITE})
    Args = MoveArgs
    grounded = ("name", "to")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(MoveArgs, args)
        if problem := _own_words(ctx, a.name, a.to):
            return problem
        home = ctx.system.home
        source = await _source(ctx, a.name, choice)
        if isinstance(source, ToolOutcome):
            return source
        if choice and choice.startswith(TARGET):
            folders = [Path(choice.removeprefix(TARGET))]
        else:
            folders = await asyncio.to_thread(find_folders, a.to, home, ctx.system.screenshot_dir())
        if not folders:
            return ToolOutcome(StepStatus.FAILED, f"There's no folder called '{a.to}' in your home")
        if len(folders) > 1:
            if choice:  # SONU asked about the file already: one question per step
                where = ", ".join(home_relative(f, home) for f in folders[:MAX_CHOICES])
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT, f"Several '{a.to}' folders: {where} — say it with its path"
                )
            return _ask(f"'{a.to}' folder", folders, home, TARGET)
        folder = folders[0]
        if not inside_home(folder, home) or not folder.is_dir():
            return ToolOutcome(StepStatus.FAILED, "SONU only moves things into folders in your home")
        if folder.resolve() == source.parent.resolve():
            return ToolOutcome(
                StepStatus.FAILED, f"'{source.name}' is already in {home_relative(folder, home)}"
            )
        if source.is_dir() and folder.resolve().is_relative_to(source.resolve()):
            return ToolOutcome(StepStatus.FAILED, f"'{source.name}' can't go into a folder inside itself")
        target = folder / source.name
        if target.exists():
            return ToolOutcome(
                StepStatus.FAILED,
                f"{home_relative(folder, home)} already has a '{source.name}' — SONU never overwrites",
            )
        return Prepared(
            f"Move {_label(source, home)} to {home_relative(folder, home)}",
            (source, target),
            spoken=f"move {source.name} to {folder.name}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        source, target = cast("tuple[Path, Path]", prepared.payload)
        if target.exists():
            return ToolOutcome(
                StepStatus.FAILED, f"Something called '{target.name}' appeared there — nothing moved"
            )
        try:
            await asyncio.to_thread(shutil.move, source, target)
        except OSError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't move '{source.name}': {exc.strerror or exc}")
        checks = _moved(source, target)
        where = home_relative(target.parent, ctx.system.home)
        if all(c.passed for c in checks):
            return ToolOutcome(StepStatus.DONE, f"Moved '{source.name}' to {where}", checks, private=True)
        return ToolOutcome(
            StepStatus.FAILED, f"'{source.name}' didn't end up in {where}", checks, private=True
        )


# ---------------------------------------------------------------------------- files.rename
class RenameArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="the user's words for the file or folder")
    new_name: str = Field(min_length=1, max_length=120, description="exactly the new name the user said")


def new_name_for(source: Path, said: str) -> str | None:
    """The new name: the user's words, keeping a file's extension if they didn't say one. None: not a
    name SONU will give (a path, hidden, empty)."""
    name = " ".join(said.strip().strip("'\"").split())
    if (
        not name
        or "/" in name
        or "\0" in name
        or name.startswith(".")
        or name in (".", "..")
        or len(name) > 200
    ):
        return None
    if source.is_file() and source.suffix and not Path(name).suffix:
        name += source.suffix
    return name


class RenameFile(Tool):
    name = "files.rename"
    description = (
        "Rename a file or folder to the name the user says ('rename notes.txt to ideas'); asks first."
    )
    risk = Risk.MEDIUM
    effects = frozenset({Effect.FILE_WRITE})
    Args = RenameArgs
    grounded = ("name", "new_name")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(RenameArgs, args)
        if problem := _own_words(ctx, a.name, a.new_name):
            return problem
        home = ctx.system.home
        source = await _source(ctx, a.name, choice)
        if isinstance(source, ToolOutcome):
            return source
        name = new_name_for(source, a.new_name)
        if name is None:
            return ToolOutcome(StepStatus.FAILED, f"'{a.new_name}' isn't a name SONU will give a file")
        target = source.with_name(name)
        if target.exists():
            return ToolOutcome(StepStatus.FAILED, f"There's already a '{name}' there — SONU never overwrites")
        return Prepared(
            f"Rename {_label(source, home)} to '{name}'",
            (source, target),
            spoken=f"rename {source.name} to {name}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        source, target = cast("tuple[Path, Path]", prepared.payload)
        if target.exists():
            return ToolOutcome(
                StepStatus.FAILED, f"Something called '{target.name}' appeared — nothing renamed"
            )
        try:
            await asyncio.to_thread(source.rename, target)
        except OSError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't rename '{source.name}': {exc.strerror or exc}")
        checks = _moved(source, target)
        if all(c.passed for c in checks):
            return ToolOutcome(
                StepStatus.DONE, f"Renamed '{source.name}' to '{target.name}'", checks, private=True
            )
        return ToolOutcome(StepStatus.FAILED, f"'{source.name}' wasn't renamed", checks, private=True)


# ---------------------------------------------------------------------------- files.trash
class TrashArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="the user's words for the file or folder")


class TrashFile(Tool):
    name = "files.trash"
    description = (
        "Delete a file or folder by moving it to the trash ('delete my last download'); it can be restored. "
        "Asks first; never deletes for good."
    )
    risk = Risk.MEDIUM  # a folder: high
    effects = frozenset({Effect.FILE_WRITE})
    Args = TrashArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(TrashArgs, args)
        if problem := _own_words(ctx, a.name):
            return problem
        if not ctx.system.available("gio"):
            return ToolOutcome(
                StepStatus.FAILED,
                "gio isn't installed — SONU only deletes into the trash (sudo pacman -S glib2)",
            )
        source = await _source(ctx, a.name, choice)
        if isinstance(source, ToolOutcome):
            return source
        folder = source.is_dir()
        return Prepared(
            f"Move {_label(source, ctx.system.home)} to the trash (it can be restored)",
            source,
            risk=Risk.HIGH if folder else None,  # everything in it goes too
            spoken=f"put {source.name} in the trash",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        source = cast(Path, prepared.payload)
        try:
            await ctx.system.trash(source)
            listed = any(original == source for _, original in await ctx.system.trashed())
        except (CommandError, ProgramMissing) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't move '{source.name}' to the trash: {exc}")
        checks = (
            Check("it's gone from where it was", not source.exists()),
            Check("it's in the trash", listed),
        )
        if all(c.passed for c in checks):
            return ToolOutcome(
                StepStatus.DONE,
                f"Moved '{source.name}' to the trash — say “restore {source.name}” to get it back",
                checks,
                private=True,
            )
        return ToolOutcome(StepStatus.FAILED, f"'{source.name}' didn't go to the trash", checks, private=True)


# ---------------------------------------------------------------------------- files.restore
class RestoreArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="the user's words for what to get back")


class RestoreFile(Tool):
    name = "files.restore"
    description = (
        "Get a file or folder back from the trash to where it was ('restore notes.txt', 'undelete it')."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.FILE_WRITE})
    Args = RestoreArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(RestoreArgs, args)
        if problem := _own_words(ctx, a.name):
            return problem
        if not ctx.system.available("gio"):
            return ToolOutcome(StepStatus.FAILED, "gio isn't installed (sudo pacman -S glib2)")
        try:
            items = await ctx.system.trashed()
        except (CommandError, ProgramMissing) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't look in the trash: {exc}")
        home = ctx.system.home
        if choice:
            found = [(u, p) for u, p in items if u == choice]
        else:
            words, kinds = file_words(a.name)
            keys = [normalize(w).replace(" ", "") for w in words]
            found = [
                (u, p)
                for u, p in items
                if all(k in normalize(p.name).replace(" ", "") for k in keys)
                and (not kinds or p.suffix.lower() in kinds)
            ]
        if not found:
            return ToolOutcome(StepStatus.FAILED, f"Nothing called '{a.name}' is in the trash")
        if len(found) > 1:
            options = tuple(
                Choice(
                    f"{p.name} — {home_relative(p.parent, home)} ({unquote(u.removeprefix('trash:///'))})", u
                )
                for u, p in found[:MAX_CHOICES]
            )
            question = f"Which '{a.name}'?"
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                question,
                question=question,
                choices=Choices(question, options),
                private=True,
            )
        uri, original = found[0]
        if original.exists():
            return ToolOutcome(
                StepStatus.FAILED, f"There's a '{original.name}' where it was — SONU never overwrites"
            )
        return Prepared(
            f"Restore '{original.name}' to {home_relative(original.parent, home)}", (uri, original)
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        uri, original = cast("tuple[str, Path]", prepared.payload)
        try:
            await ctx.system.restore(uri)
        except (CommandError, ProgramMissing) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't restore '{original.name}': {exc}")
        back = Check("it's back where it was", original.exists())
        where = home_relative(original.parent, ctx.system.home)
        if back.passed:
            return ToolOutcome(
                StepStatus.DONE, f"Restored '{original.name}' to {where}", (back,), private=True
            )
        return ToolOutcome(
            StepStatus.FAILED, f"'{original.name}' isn't back in {where}", (back,), private=True
        )


FILE_CHANGE_TOOLS: tuple[Tool, ...] = (MoveFile(), RenameFile(), TrashFile(), RestoreFile())
