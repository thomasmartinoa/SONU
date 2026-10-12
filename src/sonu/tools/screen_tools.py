""" "What's on my screen?" / "read the error in this window" — the screen read by a local vision model
(M6 part 1, D55). Read-only; the picture exists only while it's read (a 0700 runtime folder) and never
leaves the laptop; the answer is shown to the user, never given to SONU's planner.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import replace
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.control import TITLE as CONTROL_TITLE
from sonu.core.screen_reader import ScreenReaderError
from sonu.desktop.apps.resolver import normalize
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
from sonu.tools.browser_tools import FillOnPage, fill_in_window
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.tools.naming import RISKY_NAMES, name_score
from sonu.tools.targets import WindowTarget, find_windows
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot


def _visible(window: Window, snapshot: WorldSnapshot) -> bool:
    """On a workspace some monitor shows right now (grim can only capture what's on screen)."""
    shown = {m.active_workspace.id for m in snapshot.monitors} | {
        m.special_workspace.id for m in snapshot.monitors if m.special_workspace and m.special_workspace.id
    }
    return window.workspace.id in shown and window.mapped and not window.hidden


async def _show(ctx: ToolContext, window: Window) -> Window | ToolOutcome:
    """The window, on screen: brought into view (focused) first if it's on a hidden workspace."""
    if _visible(window, await ctx.desktop.snapshot()):
        return window
    try:
        await ctx.desktop.focus(window)
    except HyprlandError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't show {window.window_class}: {exc}")
    address = window.address
    shown, _ = await ctx.desktop.wait_for(
        lambda s: next((w for w in s.windows if w.address == address and _visible(w, s)), None),
        timeout_s=ctx.settings.verify.timeout_s,
    )
    return shown or ToolOutcome(StepStatus.FAILED, f"Couldn't bring {window.window_class} into view")


async def _picture(ctx: ToolContext, window: Window | None) -> bytes:
    """A picture of the window (or the screen), kept only in memory: the file is deleted at once."""
    folder = runtime_folder().parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    picture = folder / f"screen-{secrets.token_hex(4)}.png"
    argv = ["grim"]
    if window is not None:
        (x, y), (w, h) = window.at, window.size
        argv += ["-g", f"{x},{y} {w}x{h}"]
    try:
        result = await ctx.system.runner.run((*argv, str(picture)), timeout_s=10.0)
        if not result.ok:
            raise CommandError(result.problem())
        return await asyncio.to_thread(picture.read_bytes)
    finally:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(picture.unlink, missing_ok=True)


def _app_named(ctx: ToolContext, snapshot: WorldSnapshot) -> WindowTarget | None:
    """The one open app the user's words name ("what is open in zen?"), when the plan named no window.

    Found in the evals (twice, after prompt changes): the model left the window out and the whole screen
    was read. SONU decides from the user's words, like the workspace in dev.claude_open."""
    said = f" {normalize(ctx.request_text)} "
    labels = ctx.resolver.labels_for_classes(w.window_class for w in snapshot.windows if w.workspace.id >= 1)
    named = set()
    for window_class, label in labels.items():
        words = {normalize(window_class), normalize(label.split(",")[0]).split(" ")[0]}
        if any(word and len(word) > 2 and f" {word} " in said for word in words):
            named.add(normalize(label.split(",")[0]).split(" ")[0])
    return WindowTarget(app=named.pop()) if len(named) == 1 else None


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        max_length=300,
        description="what the user wants to know about the screen, in their words",
    )
    window: WindowTarget | None = Field(default=None, description="a window; none: the whole screen")


def sonus_own(window: Window) -> ToolOutcome | None:
    """SONU's own Control Center (D90): only the user changes SONU's settings — never its screen tools (the
    model, or a page through it, could otherwise press "Yes, change it")."""
    if window.title == CONTROL_TITLE:
        return ToolOutcome(
            StepStatus.DENIED, "That's SONU's own Control Center — only you can change SONU's settings there"
        )
    return None


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
        target = a.window or _app_named(ctx, snapshot)
        if target is None:
            return Prepared("Look at the screen", (None, a.question))
        match = find_windows(target, snapshot, ctx)
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
        if window is not None:
            shown = await _show(ctx, window)
            if isinstance(shown, ToolOutcome):
                return shown
            window = shown
        what = f"the {window.window_class} window" if window else "the user's screen"
        try:
            png = await _picture(ctx, window)
            answer = await reader.read(png, question, what)
        except (CommandError, ProgramMissing, OSError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't take the picture: {exc}")
        except ScreenReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the screen: {exc}")
        # From the screen: for the user's eyes (and ears), never for SONU's planner or reply writer.
        return ToolOutcome(
            StepStatus.DONE,
            answer,
            (Check("the local model read it", True, {"looked_at": what}),),
            private=True,
        )


# ---------------------------------------------------------------------------- screen.press
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
        if refused := sonus_own(window):
            return refused
        if not window.pid:
            return ToolOutcome(StepStatus.FAILED, f"Can't reach {window.window_class}'s buttons")
        try:
            controls = await Accessibility(ctx.system.runner).controls(window.pid)
        except AccessibilityError as exc:
            # The ladder's last rung (M6 part 3, D58): look at the window and click where it is.
            if ctx.reader is not None and ctx.which("wlrctl") and ctx.which("grim"):
                return Prepared(
                    f"Click “{a.control}” in {window.window_class} — it doesn't show its buttons to SONU, so "
                    "SONU will look for it on screen and click there (it can miss)",
                    ("pointer", window, a.control),
                    risk=Risk.HIGH,
                    spoken=f"click {a.control} in {window.window_class}, found by looking — it may miss",
                )
            return ToolOutcome(StepStatus.FAILED, f"{window.window_class} {exc}")
        usable = [c for c in controls if c.enabled and c.action]
        if choice:
            picked = [c for c in usable if c.path == choice]
        else:
            scored = sorted(
                ((name_score(c.name, a.control), c) for c in usable), key=lambda i: (-i[0], not i[1].showing)
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
        risky = bool(RISKY_NAMES.search(control.name))
        return Prepared(
            f"Press {control.label} in {window.window_class}",
            ("a11y", window, control),
            risk=Risk.HIGH if risky else None,
            spoken=f"press {control.name} in {window.window_class}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        if prepared.payload[0] == "pointer":
            _, window, target = cast("tuple[str, Window, str]", prepared.payload)
            return await _click_by_looking(ctx, window, target)
        _, window, control = cast("tuple[str, Window, Control]", prepared.payload)
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


async def _click_by_looking(ctx: ToolContext, window: Window, target: str) -> ToolOutcome:
    """Find ``target`` in a picture of the window, put the pointer there and click; done only if the
    window changed (M6 part 3, D58)."""
    if ctx.reader is None:
        return ToolOutcome(StepStatus.FAILED, "There's no vision model to look with")
    shown = await _show(ctx, window)
    if isinstance(shown, ToolOutcome):
        return shown
    window = shown
    try:
        before = await _picture(ctx, window)
        spot = await ctx.reader.locate(before, f"the {target}")
    except (CommandError, ProgramMissing, OSError) as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't take the picture: {exc}")
    except ScreenReaderError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't look for it: {exc}")
    if spot is None:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't find “{target}” in {window.window_class}'s window")
    (x, y), (w, h) = window.at, window.size
    point = (round(x + spot[0] * w), round(y + spot[1] * h))
    try:
        await ctx.desktop.move_cursor(*point)
        result = await ctx.system.runner.run(("wlrctl", "pointer", "click", "left"), timeout_s=5.0)
        if not result.ok:
            raise CommandError(result.problem())
        await asyncio.sleep(SETTLE_S)
        after = await _picture(ctx, window)
    except (HyprlandError, CommandError, ProgramMissing, OSError) as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't click: {exc}")
    changed = after != before
    checks = [Check("the window changed", changed, {"clicked_at": point})]
    if not changed:
        return ToolOutcome(
            StepStatus.UNVERIFIED,
            f"Clicked where “{target}” looked to be in {window.window_class}, but nothing changed — "
            "it may have missed",
            tuple(checks),
        )
    return outcome_from_checks(
        checks, f"Clicked where “{target}” looked to be in {window.window_class} (the window changed)", ""
    )


# ---------------------------------------------------------------------------- screen.type
class TypeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500, description="exactly what to type, the user's words")
    window: WindowTarget | None = Field(default=None, description="the app's window; none: the focused one")
    enter: bool = Field(default=False, description="press Enter after it, only if the user said so")


class TypeText(Tool):
    name = "screen.type"
    description = (
        "Type the user's words into an app as keystrokes (wherever its text cursor is). For apps SONU has "
        "no other tool for; never a password."
    )
    risk = Risk.HIGH  # keystrokes go to wherever the focus is: always shown and asked
    effects = frozenset({Effect.UI_INPUT})
    Args = TypeArgs
    grounded = ("text",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(TypeArgs, args)
        if missing := ungrounded_words(a.text, ctx.user_words or ctx.request_text):
            return ToolOutcome(
                StepStatus.FAILED, f"What to type must be your words ({', '.join(missing)} isn't)"
            )
        no_wtype = ToolOutcome(StepStatus.FAILED, "wtype isn't installed (it types the keys)")
        opened = [w for a_ in reversed(ctx.task_windows) for w in snapshot.windows if w.address == a_]
        into_new = a.window is None or (a.window.focused and not a.window.app)
        if into_new and not opened and ctx.task_would_open:
            if not ctx.which("wtype"):
                return no_wtype
            # Found on the laptop: the dry run of "open a terminal on 3 and type hello" said "into zen".
            then = " and press Enter" if a.enter else ""
            step = ctx.task_would_open[-1]
            return Prepared(f"Type “{a.text}” as keystrokes{then} into the window from “{step}”", None)
        if opened and into_new:
            window = opened[0]  # "open a terminal on 3 and type hello": the one this request opened
        else:
            match = find_windows(a.window or WindowTarget(focused=True), snapshot, ctx)
            if match.problem or not match.windows:
                return ToolOutcome(
                    StepStatus.FAILED, f"Can't type there: {match.problem or 'no such window'}"
                )
            window = match.windows[0]
        if refused := sonus_own(window):
            return refused
        page = await fill_in_window(ctx, window, a.text, a.enter, choice)
        if isinstance(page, Prepared):
            return replace(page, payload=("page", page.payload))
        if page is not None:
            return page
        if not ctx.which("wtype"):
            return no_wtype
        then = " and press Enter" if a.enter else ""
        return Prepared(
            f"Type “{a.text}” into {window.window_class} as keystrokes{then}",
            (window, a.text, a.enter),
            spoken=f"type {a.text} into {window.window_class}{then}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        if prepared.payload and prepared.payload[0] == "page":  # a field on a page, through the extension
            return await FillOnPage().execute(replace(prepared, payload=prepared.payload[1]), ctx)
        window, text, enter = cast("tuple[Window, str, bool]", prepared.payload)
        shown = await _show(ctx, window)
        if isinstance(shown, ToolOutcome):
            return shown
        try:
            await ctx.desktop.focus(shown)
            snapshot = await ctx.desktop.snapshot()
            # Keystrokes go to the focused window: checked right before typing (D58).
            if snapshot.active_address != shown.address:
                return ToolOutcome(StepStatus.FAILED, f"{window.window_class} isn't focused — nothing typed")
            before = await _picture(ctx, shown)
            typed = await ctx.system.runner.run(("wtype", "-"), timeout_s=10.0, input_bytes=text.encode())
            if typed.ok and enter:
                typed = await ctx.system.runner.run(("wtype", "-k", "Return"), timeout_s=5.0)
            if not typed.ok:
                raise CommandError(typed.problem())
            await asyncio.sleep(SETTLE_S)
            after = await _picture(ctx, shown)
        except (HyprlandError, CommandError, ProgramMissing, OSError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't type: {exc}")
        check = Check("the window changed", after != before)
        if not check.passed:
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Typed it into {window.window_class}, but nothing changed on screen",
                (check,),
            )
        return outcome_from_checks([check], f"Typed it into {window.window_class}", "")


SETTLE_S = 0.6  # the app redraws after a click or keystrokes
SCREEN_TOOLS: tuple[Tool, ...] = (ReadScreen(), PressControl(), TypeText())
