"""Suspend, reboot, shut down, log out — exactly as the rice's power menu does them.

* High risk: SONU always asks first, in every autonomy mode, and the question names the open
  windows (unsaved work in them would be lost).
* ``systemctl suspend / reboot / poweroff`` and Hyprland's ``exit`` dispatcher, like the power
  menu (``quickshell/Power/PowerMenu.qml``).
* SONU can't watch the laptop go to sleep or turn off, so a power step is at best "requested"
  (unverified) — never "done". If something holds a sleep or shutdown lock, systemd refuses and
  SONU says who.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.hyprland.errors import HyprlandError
from sonu.system.commands import CommandError, ProgramMissing
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.world.snapshot import WorldSnapshot

PowerChoice = Literal["suspend", "reboot", "shutdown", "logout"]
_WORDS: dict[PowerChoice, tuple[str, str]] = {
    "suspend": ("Suspend the laptop", "Suspending"),
    "reboot": ("Restart the laptop", "Restarting"),
    "shutdown": ("Shut down the laptop", "Shutting down"),
    "logout": ("Log out of Hyprland", "Logging out"),
}


def open_windows(snapshot: WorldSnapshot) -> str:
    """ "kitty ×2, zen, Thunar" — what would be closed (titles left out: they're personal)."""
    # Every window, the terminal SONU runs in too: it closes as well.
    counts = Counter(window.window_class for window in snapshot.windows)
    return ", ".join(f"{name} ×{count}" if count > 1 else name for name, count in counts.most_common())


class PowerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PowerChoice = Field(description="suspend (sleep), reboot (restart), shutdown (power off), logout")


class SystemPower(Tool):
    name = "system.power"
    description = "Suspend (sleep), restart, shut down the laptop, or log out. Always asks first."
    risk = Risk.HIGH
    effects = frozenset({Effect.POWER})
    Args = PowerArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        action = cast(PowerArgs, args).action
        if action != "logout" and not ctx.system.available("systemctl"):
            return ToolOutcome(StepStatus.FAILED, str(ProgramMissing("systemctl")))
        description = _WORDS[action][0]
        windows = open_windows(snapshot)
        if windows and action != "suspend":
            description += f" — open windows will close, unsaved work is lost: {windows}"
        return Prepared(description, action)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        action = cast(PowerChoice, prepared.payload)
        try:
            if action == "logout":
                await ctx.desktop.exit_session()
            else:
                await ctx.system.power("poweroff" if action == "shutdown" else action)
        except (CommandError, HyprlandError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"{_WORDS[action][0]} was refused: {exc}")
        return ToolOutcome(
            StepStatus.UNVERIFIED, f"{_WORDS[action][1]} now (requested — SONU can't watch it happen)"
        )


POWER_TOOLS: tuple[Tool, ...] = (SystemPower(),)
