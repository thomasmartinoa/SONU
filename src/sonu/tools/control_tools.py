"""Opening SONU's Control Center by voice (D90): "open SONU settings", "show your control center".

Opening it is all SONU does: what changes there is the user's own click — SONU's screen tools refuse to act on
that window (``screen_tools.sonus_own``), and no tool changes SONU's settings.
"""

from __future__ import annotations

import asyncio
from typing import cast

from pydantic import BaseModel, ConfigDict

from sonu.control import TITLE, shell_path, sonu_program
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot


def _open(snapshot: WorldSnapshot) -> Window | None:
    return next((window for window in snapshot.windows if window.title == TITLE), None)


class OpenControl(Tool):
    name = "control.open"
    description = (
        "Open SONU's own Control Center window (its settings: the brain, voice, heads-ups, privacy) — 'open "
        "SONU settings', 'show your control center'. The user changes things there; SONU only opens it."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH})

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if (window := _open(snapshot)) is not None:
            return Prepared("Show SONU's Control Center (already open)", window)
        qs = ctx.which("qs")
        if qs is None:
            return ToolOutcome(
                StepStatus.FAILED, "The Control Center needs Quickshell — sudo pacman -S quickshell"
            )
        argv = ("env", f"SONU_BIN={sonu_program()}", qs, "-n", "-p", str(shell_path()))
        return Prepared("Open SONU's Control Center", argv)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        try:
            if isinstance(prepared.payload, Window):
                await ctx.desktop.focus(prepared.payload)
            else:
                await ctx.desktop.launch(cast("tuple[str, ...]", prepared.payload))
        except HyprlandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Hyprland refused: {exc}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ctx.settings.apps.launch_timeout_s
        while True:
            window = _open(await ctx.desktop.snapshot())
            if window is not None or loop.time() > deadline:
                break
            await asyncio.sleep(0.2)
        check = Check("SONU's Control Center is on the screen", window is not None, {})
        if window is None:
            return ToolOutcome(StepStatus.FAILED, "The Control Center didn't open", (check,))
        return ToolOutcome(StepStatus.DONE, "SONU's Control Center is open", (check,))


CONTROL_TOOLS: tuple[Tool, ...] = (OpenControl(),)
