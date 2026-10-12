"""A briefing (D83): "good morning", "brief me", "catch me up" — the day so far in a few lines: the battery,
today's reminders, unread messages, kept notifications, what the user worked on yesterday, and anything that
crashed or failed.

Built from SONU's own read-only tools (each read and checked as it is when asked on its own), so it never says
more than they found; a part that can't be read is left out. Private: it's the user's day. sonud can also
show it once a day by itself (``[briefing] daily``, off by default).
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.everyday_tools import ListReminders
from sonu.tools.health_tools import Problems
from sonu.tools.journal_tools import ReadJournal
from sonu.tools.message_tools import UnreadMessages
from sonu.tools.settings_tools import SystemStatus
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot


def _greeting(now: datetime) -> str:
    part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
    return f"Good {part} — {now:%A %d %B}, {now:%H:%M}."


async def _part(tool: Tool, args: dict[str, Any], ctx: ToolContext, snapshot: WorldSnapshot) -> str | None:
    """One read-only tool's answer, or None when it can't say (left out, never guessed)."""
    with contextlib.suppress(Exception):  # a part that fails is left out; the others still count
        prepared = await tool.prepare(tool.parse_args(args), ctx, snapshot)
        if isinstance(prepared, Prepared):
            outcome = await tool.execute(prepared, ctx)
            if outcome.status is StepStatus.DONE and outcome.summary:
                return outcome.summary
    return None


class Briefing(Tool):
    name = "system.briefing"
    description = (
        "A short briefing: battery, today's reminders, unread messages, kept notifications, yesterday's "
        "work, "
        "anything that crashed ('good morning', 'brief me', 'catch me up', 'what's my day like?')."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        return Prepared("Put together a briefing (battery, reminders, messages, yesterday, problems)", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        return await brief(ctx)


async def brief(ctx: ToolContext) -> ToolOutcome:
    """The briefing itself — also what sonud shows once a day when it's turned on."""
    snapshot = await ctx.desktop.snapshot()
    parts = [_greeting(ctx.now())]
    asked = [
        (SystemStatus(), {"what": ["battery"]}),
        (ListReminders(), {}),
        (UnreadMessages(), {}),
    ]
    for tool, args in asked:
        if (said := await _part(tool, args, ctx, snapshot)) is not None:
            parts.append(said)
    if ctx.notifications is not None and (kept := ctx.notifications.recent()):
        apps = ", ".join(dict.fromkeys(n.app for n in kept))
        parts.append(f"{len(kept)} notification{'s' if len(kept) != 1 else ''} kept ({apps})")
    if ctx.journal is not None and (
        yesterday := await _part(ReadJournal(), {"when": "yesterday"}, ctx, snapshot)
    ):
        # found on the laptop: "Yesterday: Yesterday (10 hours …)" — the journal says the day itself
        parts.append(yesterday if yesterday.lower().startswith("yesterday") else f"Yesterday: {yesterday}")
    problems = await _part(Problems(), {}, ctx, snapshot)
    if problems and not problems.startswith("No failed services · nothing crashed today"):
        parts.append(problems)  # only when something went wrong
    told = len(parts) - 1
    check = Check("the briefing's parts were read", told > 0, {"parts": told})
    if told == 0:
        return ToolOutcome(StepStatus.FAILED, "Nothing to brief you on could be read", (check,))
    # The user's day (messages, notifications, their work): for them only.
    return ToolOutcome(StepStatus.DONE, " ".join(_sentence(p) for p in parts), (check,), private=True)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


BRIEFING_TOOLS: tuple[Tool, ...] = (Briefing(),)
