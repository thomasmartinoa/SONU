"""The rice's theme and wallpaper, and the clipboard.

* Theme and wallpaper go through the rice's own commands — ``hypr-theme`` and ``hypr-wall``, the
  same ones its picker and keys use — and are read back with ``hypr-theme current`` /
  ``hypr-wall current``. SONU picks the theme or picture from what exists (the model only passes
  the user's words), so nothing else can be set.
* The clipboard is read only when asked and shown only to the user: it never goes to the model,
  the audit log or anywhere else.
"""

from __future__ import annotations

import os
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.resolver import normalize
from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import IMAGE_SUFFIXES, Theme
from sonu.tools.base import (
    Choice,
    Choices,
    Effect,
    Prepared,
    Risk,
    StepStatus,
    Tool,
    ToolOutcome,
    outcome_from_checks,
)
from sonu.tools.context import ToolContext
from sonu.tools.web_tools import compact
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_CHOICES = 6
_THEME_FILLERS = {"theme", "the", "my", "to", "colour", "color", "scheme", "rice"}
_PICTURE_FILLERS = {"wallpaper", "background", "the", "my", "to", "picture", "image", "photo", "one", "of"}


def _missing(ctx: ToolContext, program: str, what: str) -> ToolOutcome | None:
    if ctx.system.available(program):
        return None
    return ToolOutcome(
        StepStatus.FAILED, f"{program} isn't installed — {what} comes from the hypr-dotfiles rice"
    )


def _pick(words: str, names: list[str], fillers: set[str]) -> list[str]:
    """Names the user's words point at: an exact name first, then names containing every word, then
    close spellings ("tokyo" → tokyo-night, "catpuccin" → catppuccin-mocha)."""
    wanted = [word for word in normalize(words).split() if word not in fillers]
    if not wanted:
        return []
    key = "".join(wanted)
    exact = [name for name in names if compact(name) == key]
    if exact:
        return exact
    containing = [name for name in names if all(word in compact(name) for word in wanted)]
    if containing:
        return containing
    return [
        name
        for name in names
        if any(
            SequenceMatcher(None, word, part).ratio() >= 0.8
            for word in wanted
            if len(word) >= 4
            for part in normalize(name).split()
        )
    ]


def named_theme(request: str, themes: list[Theme]) -> Theme | None:
    """The one theme the user's own words name ("switch to the tokyo night theme"), if any.

    Found in an eval: that request was planned as "next theme". The most specific name wins:
    "hyprmono light" names hyprmono-light, not hyprmono.
    """
    words = set(normalize(request).split())
    named = [(len(parts), t) for t in themes if (parts := set(normalize(t.id).split())) and parts <= words]
    if not named:
        return None
    most = max(size for size, _ in named)
    best = [t for size, t in named if size == most]
    return best[0] if len(best) == 1 else None


# ---------------------------------------------------------------------------- desktop.theme
class ThemeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["set", "next", "light", "dark"] = Field(
        description="set (a theme the user named), next, light ('light mode'), dark ('dark mode')"
    )
    name: str | None = Field(
        default=None, max_length=60, description="for set: the theme as the user named it"
    )


class DesktopTheme(Tool):
    name = "desktop.theme"
    description = "Switch the rice's colour theme: by name, the next one, or light / dark mode."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ThemeArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ThemeArgs, args)
        if problem := _missing(ctx, "hypr-theme", "the theme"):
            return problem
        try:
            themes = await ctx.system.themes()
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the themes: {exc}")
        current = next((t for t in themes if t.current), None)
        if a.action == "next" and (named := named_theme(ctx.request_text, themes)) is not None:
            return Prepared(f"Switch the theme to {named.id}", ("set", named.id, current))
        if a.action == "next":
            return Prepared("Switch to the next theme", ("next", None, current))
        if a.action in ("light", "dark"):
            return Prepared(f"Switch to a {a.action} theme", (a.action, None, current))
        ids = [t.id for t in themes]
        if choice is not None:
            matches = [t for t in themes if t.id == choice]
        elif not a.name:
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                "no theme named",
                question=f"Which theme: {', '.join(ids[:MAX_CHOICES])}?",
                choices=Choices("Which theme?", tuple(Choice(t.name, t.id) for t in themes[:MAX_CHOICES])),
            )
        else:
            # The user may say the id ("tokyo-night") or the name ("Tokyo Night").
            picked = set(_pick(a.name, ids, _THEME_FILLERS)) or {
                t.id for t in themes if t.name in _pick(a.name, [t.name for t in themes], _THEME_FILLERS)
            }
            matches = [t for t in themes if t.id in picked]
        if not matches:
            return ToolOutcome(
                StepStatus.FAILED, f"There's no theme like '{a.name}' (themes: {', '.join(ids)})"
            )
        if len(matches) > 1:
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"several themes match '{a.name}'",
                question=f"Which theme: {', '.join(t.id for t in matches[:MAX_CHOICES])}?",
                choices=Choices("Which theme?", tuple(Choice(t.name, t.id) for t in matches[:MAX_CHOICES])),
            )
        return Prepared(f"Switch the theme to {matches[0].id}", ("set", matches[0].id, current))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        action, target, before = cast(tuple[str, str | None, Theme | None], prepared.payload)
        if action in ("light", "dark") and before is not None and before.mode == action:
            check = Check(f"the theme is {action}", True, {"theme": before.id, "mode": before.mode})
            return outcome_from_checks([check], f"Already using a {action} theme ({before.id})", "")
        try:
            if action == "set":
                await ctx.system.switch_theme("set", target)
            else:
                await ctx.system.switch_theme("next" if action == "next" else "toggle")
            now = next((t for t in await ctx.system.themes() if t.current), None)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"The theme didn't switch: {exc}")
        if action == "set":
            passed, wanted = now is not None and now.id == target, f"the theme is {target}"
        elif action == "next":
            passed, wanted = now is not None and (before is None or now.id != before.id), "the theme changed"
        else:
            passed, wanted = now is not None and now.mode == action, f"the theme is {action}"
        evidence = {"before": before.id if before else None, "now": now.id if now else None}
        name = now.id if now else "unknown"
        return outcome_from_checks(
            [Check(wanted, passed, evidence)], f"Switched the theme to {name}", f"The theme is {name}"
        )


# ---------------------------------------------------------------------------- desktop.wallpaper
class WallpaperArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["next", "set"] = Field(
        description="next (the theme's next wallpaper), set (a picture the user named)"
    )
    name: str | None = Field(
        default=None, max_length=80, description="for set: the picture as the user named it, e.g. 'mountain'"
    )


def _pictures(ctx: ToolContext) -> list[Path]:
    """Pictures a wallpaper can be: the theme's backgrounds, then ~/Pictures (two levels)."""
    found = list(ctx.system.theme_backgrounds())
    pictures = ctx.system.home / "Pictures"
    for root, folders, files in os.walk(pictures):
        depth = len(Path(root).relative_to(pictures).parts)
        folders[:] = [f for f in folders if not f.startswith(".")] if depth < 1 else []
        found += [Path(root) / f for f in sorted(files) if Path(f).suffix.lower() in IMAGE_SUFFIXES]
    return found


class Wallpaper(Tool):
    name = "desktop.wallpaper"
    description = "Change the wallpaper: the theme's next one, or a picture the user named."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE, Effect.FILE_READ})
    Args = WallpaperArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(WallpaperArgs, args)
        if problem := _missing(ctx, "hypr-wall", "the wallpaper"):
            return problem
        if a.action == "next" or not a.name:
            return Prepared("Switch to the theme's next wallpaper", None)
        pictures = _pictures(ctx)
        by_name = {str(p): p for p in pictures}
        if choice is not None:
            matches = [by_name[choice]] if choice in by_name else []
        else:
            stems = _pick(a.name, [p.stem for p in pictures], _PICTURE_FILLERS)
            matches = [p for p in pictures if p.stem in stems]
        if not matches:
            return ToolOutcome(
                StepStatus.FAILED, f"There's no picture like '{a.name}' in the theme or ~/Pictures"
            )
        if len(matches) > 1:
            shown = matches[:MAX_CHOICES]
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"several pictures match '{a.name}'",
                question=f"Which picture: {', '.join(p.name for p in shown)}?",
                choices=Choices("Which picture?", tuple(Choice(p.name, str(p)) for p in shown)),
            )
        return Prepared(f"Set the wallpaper to {matches[0].name}", matches[0])

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        image = cast(Path | None, prepared.payload)
        try:
            before = await ctx.system.wallpaper()
            await ctx.system.change_wallpaper(image)
            now = await ctx.system.wallpaper()
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"The wallpaper didn't change: {exc}")
        if image is not None:
            passed = now is not None and now.resolve() == image.resolve()
        else:
            passed = now is not None and now != before
        check = Check("the wallpaper changed", passed, {"now": now.name if now else None})
        name = now.name if now else "?"
        return outcome_from_checks([check], f"The wallpaper is now {name}", f"The wallpaper is still {name}")


# ---------------------------------------------------------------------------- clipboard.read / clear
class ClipboardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadClipboard(Tool):
    name = "clipboard.read"
    description = "Tell the user what's on the clipboard ('what did I copy?'); shown only to them."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ClipboardArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if not ctx.system.available("wl-paste"):
            return ToolOutcome(StepStatus.FAILED, str(ProgramMissing("wl-paste")))
        return Prepared("Read the clipboard", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        try:
            types = await ctx.system.clipboard_types()
            text = await ctx.system.clipboard_text() if types else None
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the clipboard: {exc}")
        if not types:
            return ToolOutcome(StepStatus.DONE, "The clipboard is empty")
        if text is None:
            kind = next((t for t in types if "/" in t), types[0])
            return ToolOutcome(StepStatus.DONE, f"The clipboard has {kind} (not text)", private=True)
        # Private: shown to the user only — never to the model, never in the audit log.
        return ToolOutcome(StepStatus.DONE, f"The clipboard has: {text}", private=True)


class ClearClipboard(Tool):
    name = "clipboard.clear"
    description = "Empty the clipboard."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ClipboardArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if not ctx.system.available("wl-copy"):
            return ToolOutcome(StepStatus.FAILED, str(ProgramMissing("wl-copy")))
        return Prepared("Clear the clipboard", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        try:
            await ctx.system.clear_clipboard()
            left = await ctx.system.clipboard_types()
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't clear the clipboard: {exc}")
        check = Check("the clipboard is empty", not left, {"types": left})
        return outcome_from_checks([check], "Cleared the clipboard", "The clipboard still has something")


RICE_TOOLS: tuple[Tool, ...] = (DesktopTheme(), Wallpaper(), ReadClipboard(), ClearClipboard())
