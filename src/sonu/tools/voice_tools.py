""" "Listen for Hey Sonu" / "stop listening for Hey Sonu" — see ``sonu.voice.wake`` (D47).

Turning it on keeps the microphone open, so it asks first (medium risk; by voice, a spoken yes).
Turning it off never asks. The daemon owns the listener; the check reads its state back.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome, outcome_from_checks
from sonu.tools.context import ToolContext
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot


class WakeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool = Field(description="true: start listening for 'Hey Sonu'; false: stop")


class Wake(Tool):
    name = "voice.wake"
    description = (
        "Start or stop listening for 'Hey Sonu' (the wake word), so the user can talk to SONU "
        "without pressing the key."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = WakeArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        on = cast(WakeArgs, args).on
        if ctx.wake is None:
            return ToolOutcome(
                StepStatus.FAILED,
                "Listening for 'Hey Sonu' needs the SONU daemon (sonu daemon install) and voice "
                "(sonu voice setup)",
            )
        if ctx.wake.listening == on:
            state = "Already listening" if on else "Wasn't listening"
            return ToolOutcome(
                StepStatus.DONE,
                f"{state} for 'Hey Sonu'",
                (Check("listening for 'Hey Sonu'" if on else "not listening", True),),
            )
        if on:
            return Prepared(
                "Listen for 'Hey Sonu' — the microphone stays on (checked on this laptop only) "
                "until you turn it off",
                True,
                risk=Risk.MEDIUM,
            )
        return Prepared("Stop listening for 'Hey Sonu'", False)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        on = bool(prepared.payload)
        if ctx.wake is None:
            return ToolOutcome(StepStatus.FAILED, "The SONU daemon isn't running")
        problem = await ctx.wake.set(on)
        check = Check(
            "listening for 'Hey Sonu'" if on else "not listening", ctx.wake.listening == on, {"on": on}
        )
        if problem and not check.passed:
            return ToolOutcome(StepStatus.FAILED, problem, (check,))
        if on:
            return outcome_from_checks(
                [check],
                "Listening for 'Hey Sonu' — say “stop listening for Hey Sonu” to turn it off",
                "Couldn't start listening for 'Hey Sonu'",
            )
        return outcome_from_checks(
            [check], "Stopped listening for 'Hey Sonu'", "Couldn't stop listening for 'Hey Sonu'"
        )


VOICE_TOOLS: tuple[Tool, ...] = (Wake(),)
