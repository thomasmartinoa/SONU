""" "What's on my screen?" / "read the error in this window" — the screen read by a local vision model
(M6 part 1, D55). Read-only; the picture exists only while it's read (a 0700 runtime folder) and never
leaves the laptop; the answer is shown to the user, never given to SONU's planner.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.screen_reader import ScreenReaderError
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.integrations.kitty import runtime_folder
from sonu.system.commands import CommandError, ProgramMissing
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
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


SCREEN_TOOLS: tuple[Tool, ...] = (ReadScreen(),)
