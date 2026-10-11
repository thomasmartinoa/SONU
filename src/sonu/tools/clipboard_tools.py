"""Copying to the clipboard by voice (D76): "copy the path of my invoice", "copy my kestrel lake photo", "copy
my last screenshot", "copy this page's link", "copy hello world".

What's copied comes from somewhere SONU reads itself — a file found from the user's words, the newest
screenshot, the address of the page in front (SONU's browser extension), or the user's own words — never
from a model. It stays on the laptop (``wl-copy``); file names and addresses are private results. Done when
the clipboard reads back as what was copied.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.system.commands import CommandError, ProgramMissing
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.browser_tools import _closed, _in_front, _tabs
from sonu.tools.context import ToolContext
from sonu.tools.file_change_tools import _ask, find_sources, protected
from sonu.tools.file_tools import home_relative, newest_file
from sonu.tools.grounding import ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_TEXT_FILE = 1_000_000  # bytes: a text file bigger than this is copied as its path instead
MAX_PICTURE = 50_000_000
SOURCE = "src:"
What = Literal["path", "file", "screenshot", "link", "text"]


_COPY_OF = re.compile(
    r"^\s*(?:please\s+)?copy\s+(?:the\s+)?(?:(?:full\s+)?(?:path|location)\s+(?:of|to|for)\s+)?(.+?)"
    r"(?:\s+(?:to|onto|into)\s+(?:the|my)\s+clipboard)?[.!?]*\s*$",
    re.I,
)


def _name_said(request: str) -> str | None:
    """ "copy the path of my invoice" → "my invoice"."""
    said = _COPY_OF.match(" ".join(request.split()))
    return said.group(1) if said else None


class CopyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: What = Field(
        description=(
            "path: a file's location; file: a picture's or text file's contents; screenshot: the newest one; "
            "link: the address of the web page in front; text: the user's own words"
        )
    )
    name: str | None = Field(
        default=None, max_length=160, description="for path / file: the user's words for it"
    )
    text: str | None = Field(default=None, max_length=2000, description="for text: exactly the user's words")


def _own_words(ctx: ToolContext, *values: str | None) -> ToolOutcome | None:
    for value in values:
        if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
            return ToolOutcome(StepStatus.FAILED, f"That must be your own words ({', '.join(missing)} isn't)")
    return None


class CopyToClipboard(Tool):
    name = "clipboard.copy"
    description = (
        "Copy to the clipboard: a file's path, a picture or text file, the newest screenshot, the web page's "
        "link, or the user's own words."
    )
    risk = Risk.LOW  # it replaces what was copied before
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = CopyArgs
    grounded = ("name", "text")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(CopyArgs, args)
        if problem := _own_words(ctx, a.name, a.text):
            return problem
        if not ctx.system.available("wl-copy"):
            return ToolOutcome(StepStatus.FAILED, "wl-copy isn't installed (sudo pacman -S wl-clipboard)")
        home = ctx.system.home
        if a.what == "text":
            # Found on the laptop (dry run): "copy hello from sonu" was planned with no text.
            text = a.text or _name_said(ctx.request_text)
            if not text:
                return ToolOutcome(StepStatus.FAILED, "What should I copy? Say the words")
            return Prepared(f"Copy “{text}”", (text.encode(), "text/plain;charset=utf-8", text, "your words"))
        if a.what == "link":
            found = await _tabs(ctx, None)
            if isinstance(found, ToolOutcome):
                return found
            tabs = _in_front(found.tabs, snapshot)
            if len(tabs) != 1 or _closed(tabs[0]):
                return ToolOutcome(StepStatus.FAILED, "Which page? Bring its window to the front first")
            url = tabs[0].url
            return Prepared(
                f"Copy the link of the page in {tabs[0].browser}",
                (url.encode(), "text/plain", url, "the link"),
            )
        if a.what == "screenshot":
            folder = ctx.system.screenshot_dir()
            shot = newest_file(folder) if folder and folder.is_dir() else None
            if shot is None:
                return ToolOutcome(StepStatus.FAILED, "There's no screenshot to copy")
            return await _file_contents(ctx, shot, "the newest screenshot")
        # Found in the evals (v37): "copy the path of my invoice" was planned as files.find, then a copy with
        # no name. The file the user's words name decides.
        name = a.name or _name_said(ctx.request_text)
        if not name:
            return ToolOutcome(StepStatus.FAILED, "Which file? Say its name")
        if choice and choice.startswith(SOURCE):
            sources = [Path(choice.removeprefix(SOURCE))]
        else:
            sources = await asyncio.to_thread(
                find_sources, name, ctx.request_text, home, ctx.system.screenshot_dir()
            )
        if not sources:
            return ToolOutcome(StepStatus.FAILED, f"There's no file or folder called '{name}' in your home")
        if len(sources) > 1:
            return _ask(f"'{name}'", sources, home, SOURCE)
        source = sources[0]
        if a.what == "path":
            path = str(source)
            label = f"the path of '{source.name}' ({home_relative(source.parent, home)})"
            return Prepared(f"Copy {label}", (path.encode(), "text/plain", path, label))
        if (why := protected(source, home)) and why.startswith("hidden"):
            return ToolOutcome(StepStatus.FAILED, f"SONU won't copy '{source.name}': {why}")
        return await _file_contents(ctx, source, f"'{source.name}'")

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        data, mime, expected, label = cast("tuple[bytes, str, str | None, str]", prepared.payload)
        try:
            await ctx.system.copy(data, mime)
            if expected is None:  # a picture: its type is on the clipboard
                copied = mime in await ctx.system.clipboard_types()
            else:
                copied = (await ctx.system.clipboard_text() or "").strip() == expected.strip()
        except (CommandError, ProgramMissing) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't copy {label}: {exc}")
        check = Check("the clipboard has it", copied)
        if not copied:
            return ToolOutcome(
                StepStatus.FAILED, f"The clipboard doesn't have {label}", (check,), private=True
            )
        return ToolOutcome(StepStatus.DONE, f"Copied {label}", (check,), private=True)


def _size(file: Path) -> int | None:
    """The file's size; None for a folder."""
    return None if file.is_dir() else file.stat().st_size


async def _file_contents(ctx: ToolContext, file: Path, label: str) -> Prepared | ToolOutcome:
    """A picture as a picture, a text file as text; anything else: its path is what can be copied."""
    size = await asyncio.to_thread(_size, file)
    if size is None:
        return ToolOutcome(StepStatus.FAILED, f"{label} is a folder — SONU can copy its path")
    try:
        mime = await ctx.system.mime_type(file) if ctx.system.available("xdg-mime") else None
    except CommandError:
        mime = None
    if mime and mime.startswith("image/") and size <= MAX_PICTURE:
        data = await asyncio.to_thread(file.read_bytes)
        return Prepared(f"Copy {label} (the picture)", (data, mime, None, label))
    if mime and mime.startswith("text/") and size <= MAX_TEXT_FILE:
        data = await asyncio.to_thread(file.read_bytes)
        try:
            text = data.decode()
        except UnicodeDecodeError:
            text = None
        if text is not None:
            return Prepared(
                f"Copy the text of {label}", (data, "text/plain;charset=utf-8", text, f"the text of {label}")
            )
    return ToolOutcome(
        StepStatus.FAILED, f"SONU copies pictures and text files; for {label} it can copy its path instead"
    )


CLIPBOARD_TOOLS: tuple[Tool, ...] = (CopyToClipboard(),)
