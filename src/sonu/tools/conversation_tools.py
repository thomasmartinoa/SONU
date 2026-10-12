"""Looking back at what the user and SONU said (D86): "what did we talk about yesterday?", "what did I ask you
this morning?", "what did you tell me about kestrels?" — and "forget our conversations".

Read from SONU's own conversation log (``core/talks.py``): the user's words and what SONU said back. A
private result: the user's own conversations, shown and said to them, never sent to a model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.talks import Exchange, Talks
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome, outcome_from_checks
from sonu.tools.context import ToolContext
from sonu.tools.journal_tools import When, _wanted, period
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_LISTED = 8  # a day's conversations: the latest this many
MAX_FOUND = 3  # "what did you tell me about …": the latest this many, said in full
SHORT = 120
LONG = 500
_SENTENCE = re.compile(r"(?<=[.!?])\s")


def _off(ctx: ToolContext) -> ToolOutcome | None:
    if ctx.talks is None or not ctx.settings.conversations.keep:
        return ToolOutcome(
            StepStatus.FAILED, "SONU doesn't keep conversations ([conversations] keep = false in its config)"
        )
    return None


def _cut(text: str, limit: int) -> str:
    """The first sentence (and more, up to ``limit``) — never a word cut in half."""
    if len(text) <= limit:
        return text
    first = _SENTENCE.split(text, maxsplit=1)[0]
    if len(first) <= limit:
        return first
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _when(at: datetime, now: datetime, *, day: bool) -> str:
    days = (now.date() - at.date()).days
    clock = at.strftime("%H:%M")
    if not day:
        return clock
    name = "today" if days == 0 else "yesterday" if days == 1 else at.strftime("%A %d %B")
    return f"{name} at {clock}"


class RecallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: When | None = Field(default=None, description="which part of the day; leave out with 'about'")
    about: str | None = Field(
        default=None, max_length=80, description="only what was said about this, in the user's words"
    )


class Recall(Tool):
    name = "conversations.recall"
    description = (
        "What the user and SONU said to each other earlier: 'what did we talk about yesterday?', 'what did I "
        "ask you this morning?', 'what did you tell me about kestrels?' (journal.read: which apps they used)."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = RecallArgs
    grounded = ("about",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if problem := _off(ctx):
            return problem
        a = cast(RecallArgs, args)
        words = tuple(_wanted(a.about)) if a.about else ()
        what = f" about “{a.about}”" if words else ""
        return Prepared(f"Look back at our conversations{what}", (a.when, words))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        when, words = cast("tuple[When | None, tuple[str, ...]]", prepared.payload)
        talks = cast(Talks, ctx.talks)
        now = ctx.now()
        if when is None and words:
            start, end = now - timedelta(days=ctx.settings.conversations.keep_days), now
            said = f"in the last {ctx.settings.conversations.keep_days} days"
        else:
            start, end, said = period(when or "today", now)
        found = talks.between(start.timestamp(), end.timestamp(), words)
        check = Check("the conversation log was read", True, {"found": len(found)})
        if not found:
            about = f" about “{' '.join(words)}”" if words else ""
            return ToolOutcome(StepStatus.DONE, f"We didn't talk{about} {said}.", (check,), private=True)
        if words:
            text = self._found(found[-MAX_FOUND:][::-1], now)
        else:
            text = self._listed(found, said, now, day=when == "this_week")
        # The user's own conversations: for them only.
        return ToolOutcome(StepStatus.DONE, text, (check,), private=True)

    def _found(self, found: list[Exchange], now: datetime) -> str:
        parts = [
            f"{_when(datetime.fromtimestamp(e.at), now, day=True).capitalize()} you asked “{e.request}” — "
            f"I said: {_cut(e.reply, LONG)}"
            for e in found
        ]
        return " Before that: ".join(parts)

    def _listed(self, found: list[Exchange], said: str, now: datetime, *, day: bool) -> str:
        shown = found[-MAX_LISTED:]
        items = "; ".join(
            f"{_when(datetime.fromtimestamp(e.at), now, day=day)} “{e.request}” ({_cut(e.reply, SHORT)})"
            for e in shown
        )
        earlier = f", and {len(found) - len(shown)} more" if len(found) > len(shown) else ""
        count = f"{len(found)} thing{'s' if len(found) != 1 else ''}"
        return f"{said.capitalize()} you asked me {count}{earlier}: {items}"


class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: Literal["last_hour", "today", "everything"] = Field(description="what to delete")


class ForgetTalks(Tool):
    name = "conversations.forget"
    description = "Delete what the user and SONU said to each other: the last hour, today, or everything."
    risk = Risk.MEDIUM
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ForgetArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if problem := _off(ctx):
            return problem
        since = cast(ForgetArgs, args).since
        now = ctx.now()
        at, what = {
            "last_hour": ((now - timedelta(hours=1)).timestamp(), "our conversations from the last hour"),
            "today": (
                now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
                "today's conversations",
            ),
        }.get(since, (0.0, "all our conversations"))
        count = cast(Talks, ctx.talks).count(at)
        if not count:
            return ToolOutcome(
                StepStatus.DONE, f"Nothing to delete: there are no {what.removeprefix('all ')}"
            )
        return Prepared(
            f"Delete {what} ({count} request{'s' * (count != 1)}) — it can't be undone", (at, what)
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        at, what = cast(tuple[float, str], prepared.payload)
        talks = cast(Talks, ctx.talks)
        talks.forget(at)
        check = Check("nothing left in that time", talks.count(at) == 0, {})
        return outcome_from_checks([check], f"Deleted {what}", f"Couldn't delete {what}")


CONVERSATION_TOOLS: tuple[Tool, ...] = (Recall(), ForgetTalks())
