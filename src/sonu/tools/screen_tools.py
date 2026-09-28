""" "What's on my screen?" / "read the error in this window" — the screen read by a local vision model
(M6 part 1, D55). Read-only; the picture exists only while it's read (a 0700 runtime folder) and never
leaves the laptop; the answer is shown to the user, never given to SONU's planner.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from difflib import SequenceMatcher
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.screen_reader import ScreenReaderError
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.integrations.a11y import Accessibility, AccessibilityError, Control
from sonu.integrations.kitty import runtime_folder
from sonu.system.commands import CommandError, ProgramMissing
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
from sonu.tools.grounding import ungrounded_words
from sonu.tools.targets import WindowTarget, find_windows
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot


def _visible(window: Window, snapshot: WorldSnapshot) -> bool:
    """On a workspace some monitor shows right now (grim can only capture what's on screen)."""
    shown = {m.active_workspace.id for m in snapshot.monitors} | {
        m.special_workspace.id for m in snapshot.monitors if m.special_workspace and m.special_workspace.id
    }
    return window.workspace.id in shown and window.mapped and not window.hidden


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        max_length=300,
        description="what the user wants to know about the screen, in their words",
    )
    window: WindowTarget | None = Field(default=None, description="a window; none: the whole screen")


class ReadScreen(Tool):
    name = "screen.read"
    description = (
        "Look at the screen (or one window) with the local vision model and answer the user's question "
        "about it: what's there, what an error says, what a window shows."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SCREEN_CAPTURE, Effect.WINDOW_FOCUS})  # shows the window if it's hidden
    Args = ReadArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ReadArgs, args)
        if ctx.reader is None:
            return ToolOutcome(StepStatus.FAILED, "There's no vision model to read the screen with")
        if not ctx.which("grim"):
            return ToolOutcome(StepStatus.FAILED, "grim isn't installed (it takes the picture)")
        if a.window is None:
            return Prepared("Look at the screen", (None, a.question))
        match = find_windows(a.window, snapshot, ctx)
        if match.problem or not match.windows:
            return ToolOutcome(StepStatus.FAILED, f"Can't look at it: {match.problem or 'no such window'}")
        window = match.windows[0]
        if not _visible(window, snapshot):
            # Found on the laptop: "what is open in zen?" with Zen on another workspace — the model
            # planned only the read (or only a focus). Showing the window is part of looking at it.
            return Prepared(
                f"Show {window.window_class} (workspace {window.workspace.name}) and look at it",
                (window, a.question),
            )
        return Prepared(f"Look at the {window.window_class} window", (window, a.question))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        window, question = cast("tuple[Window | None, str]", prepared.payload)
        reader = ctx.reader
        if reader is None:
            return ToolOutcome(StepStatus.FAILED, "There's no vision model to read the screen with")
        if window is not None and not _visible(window, await ctx.desktop.snapshot()):
            try:
                await ctx.desktop.focus(window)
            except HyprlandError as exc:
                return ToolOutcome(StepStatus.FAILED, f"Couldn't show {window.window_class}: {exc}")
            address = window.address
            shown, _ = await ctx.desktop.wait_for(
                lambda s: next((w for w in s.windows if w.address == address and _visible(w, s)), None),
                timeout_s=ctx.settings.verify.timeout_s,
            )
            if shown is None:
                return ToolOutcome(StepStatus.FAILED, f"Couldn't bring {window.window_class} into view")
            window = shown
        folder = runtime_folder().parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        picture = folder / f"screen-{secrets.token_hex(4)}.png"
        argv = ["grim"]
        if window is not None:
            (x, y), (w, h) = window.at, window.size
            argv += ["-g", f"{x},{y} {w}x{h}"]
        what = f"the {window.window_class} window" if window else "the user's screen"
        try:
            result = await ctx.system.runner.run((*argv, str(picture)), timeout_s=10.0)
            if not result.ok:
                return ToolOutcome(StepStatus.FAILED, f"Couldn't take the picture: {result.problem()}")
            png = await asyncio.to_thread(picture.read_bytes)
            answer = await reader.read(png, question, what)
        except (CommandError, ProgramMissing, OSError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't take the picture: {exc}")
        except ScreenReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the screen: {exc}")
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(picture.unlink, missing_ok=True)
        # From the screen: for the user's eyes (and ears), never for SONU's planner or reply writer.
        return ToolOutcome(
            StepStatus.DONE,
            answer,
            (Check("the local model read it", True, {"looked_at": what}),),
            private=True,
        )


# ---------------------------------------------------------------------------- screen.press
# Buttons whose press can't be taken back: asked like other high-risk steps (by voice: on screen).
_RISKY = re.compile(
    r"\b(delete|remove|erase|format|send|submit|post|publish|buy|pay|purchase|order|checkout|confirm|"
    r"merge|uninstall|install|sign out|log ?out|shut ?down|restart|discard|overwrite|transfer)\b",
    re.I,
)


# What people add to a button's name ("the home button"); found on the laptop: "home button" didn't
# match Thunar's "Home".
_KIND_WORDS = {
    "the",
    "a",
    "button",
    "buttons",
    "icon",
    "tab",
    "menu",
    "link",
    "item",
    "toggle",
    "switch",
    "key",
    "option",
}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _score(control: Control, wanted: str) -> int:
    name = control.name.lower().strip()
    words = [w for w in _words(wanted) if w not in _KIND_WORDS] or _words(wanted)
    wanted = " ".join(words)
    if name == wanted or _words(name) == words:
        return 100
    if words and _words(name)[: len(words)] == words:
        return 60
    if words and all(w in _words(name) for w in words):
        return 40
    if SequenceMatcher(None, name, wanted.lower()).ratio() >= 0.8:
        return 30
    return 0


class PressArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control: str = Field(min_length=1, max_length=80, description="the button's name, in the user's words")
    window: WindowTarget | None = Field(default=None, description="the app's window; none: the focused one")


class PressControl(Tool):
    name = "screen.press"
    description = (
        "Press a button (or menu, tab, toggle) in an app by its name, through accessibility — no mouse. "
        "For apps SONU has no other tool for."
    )
    risk = Risk.MEDIUM
    effects = frozenset({Effect.UI_INPUT})
    Args = PressArgs
    grounded = ("control",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(PressArgs, args)
        if missing := ungrounded_words(a.control, ctx.user_words or ctx.request_text):
            return ToolOutcome(
                StepStatus.FAILED, f"The button must be your words ({', '.join(missing)} isn't)"
            )
        target = a.window or WindowTarget(focused=True)
        match = find_windows(target, snapshot, ctx)
        if match.problem or not match.windows:
            return ToolOutcome(StepStatus.FAILED, f"Can't press it: {match.problem or 'no such window'}")
        window = match.windows[0]
        if not window.pid:
            return ToolOutcome(StepStatus.FAILED, f"Can't reach {window.window_class}'s buttons")
        try:
            controls = await Accessibility(ctx.system.runner).controls(window.pid)
        except AccessibilityError as exc:
            return ToolOutcome(StepStatus.FAILED, f"{window.window_class} {exc}")
        usable = [c for c in controls if c.enabled and c.action]
        if choice:
            picked = [c for c in usable if c.path == choice]
        else:
            scored = sorted(
                ((_score(c, a.control), c) for c in usable), key=lambda i: (-i[0], not i[1].showing)
            )
            best = scored[0][0] if scored else 0
            picked = [c for s, c in scored if s == best and best > 0]
            showing = [c for c in picked if c.showing]
            picked = showing or picked
            # The same button twice (a toolbar and a menu): one is enough if they're alike.
            if len({(c.name, c.role) for c in picked}) == 1:
                picked = picked[:1]
        if not picked:
            names = ", ".join(dict.fromkeys(c.name for c in usable if c.showing))[:300]
            # The names come from the app: shown to the user only.
            return ToolOutcome(
                StepStatus.FAILED,
                f"No “{a.control}” in {window.window_class} — it has: {names or 'no buttons it can press'}",
                private=True,
            )
        if len(picked) > 1:
            options = tuple(Choice(c.label, c.path) for c in picked[:8])
            question = f"Which “{a.control}” in {window.window_class}?"
            return ToolOutcome(
                StepStatus.NEEDS_INPUT, question, question=question, choices=Choices(question, options)
            )
        control = picked[0]
        risky = bool(_RISKY.search(control.name))
        return Prepared(
            f"Press {control.label} in {window.window_class}",
            (window, control),
            risk=Risk.HIGH if risky else None,
            spoken=f"press {control.name} in {window.window_class}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        window, control = cast("tuple[Window, Control]", prepared.payload)
        before = await ctx.desktop.snapshot()
        try:
            pressed, was, now = await Accessibility(ctx.system.runner).press(window.pid, control)
        except AccessibilityError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't press {control.label}: {exc}")
        after = await ctx.desktop.snapshot()
        title_then = next((w.title for w in before.windows if w.address == window.address), None)
        title_now = next((w.title for w in after.windows if w.address == window.address), None)
        changed = was != now or title_then != title_now or before.active_address != after.active_address
        checks = [
            Check(f"{window.window_class} took the press", pressed),
            Check("something changed", changed, {"window_title_changed": title_then != title_now}),
        ]
        if pressed and not changed:
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Pressed {control.label} in {window.window_class}, but couldn't see anything change",
                tuple(checks),
            )
        return outcome_from_checks(
            checks,
            f"Pressed {control.label} in {window.window_class}",
            f"{window.window_class} didn't take the press on {control.label}",
        )


SCREEN_TOOLS: tuple[Tool, ...] = (ReadScreen(), PressControl())
