""" "Remember …", "forget …", "what do you remember?" — see ``sonu.core.memory``.

The model only says which kind of memory it is and passes the user's own words; SONU checks the
words are the user's, resolves an app alias against the installed apps (asking if it could be
several), stores it, and reads it back from disk before saying it's remembered.
"""

from __future__ import annotations

from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sonu.core.memory import Memory, MemoryItem
from sonu.desktop.apps.resolver import normalize
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
from sonu.tools.desktop_tools import resolve_app
from sonu.tools.grounding import CONNECTIVES, ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_CHOICES = 6


def _not_the_users(ctx: ToolContext, *texts: str | None) -> ToolOutcome | None:
    """Only the user's own words are remembered (never the model's or a window's)."""
    for text in texts:
        if text and (missing := ungrounded_words(text, ctx.user_words or ctx.request_text)):
            return ToolOutcome(
                StepStatus.FAILED, f"Won't remember '{text}': {', '.join(missing)} isn't in your request"
            )
    return None


def _users_app(ctx: ToolContext, app: str) -> str:
    """The app name as the user said it, when the model shortened it. Found in an eval: "when I say vs
    code I mean code oss" was planned with app "code" (several apps match) instead of "code oss"."""
    said = f" {normalize(ctx.request_text)} "
    wanted = normalize(app)
    longer = {
        name
        for entry in ctx.resolver.catalog.entries
        if not entry.no_display
        and (name := normalize(entry.name.replace(" - ", " ")))
        and name != wanted
        and f" {wanted} " in f" {name} "
        and f" {name} " in said
    }
    return longer.pop() if len(longer) == 1 else app


def _stored(ctx: ToolContext, item: MemoryItem) -> bool:
    """Read the memory back (from disk when it has a file): is the item really kept?"""
    memory = Memory.load(ctx.memory.path) if ctx.memory.path is not None else ctx.memory
    return any((i.kind, i.text, i.app) == (item.kind, item.text, item.app) for i in memory.items)


# ---------------------------------------------------------------------------- memory.remember
class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["alias", "name", "note"] = Field(
        description="alias ('when I say X I mean app Y'), name ('call me …'), or note (anything else)"
    )
    text: str = Field(
        min_length=1,
        max_length=200,
        description="the user's words: for alias what they'll say (X), for name the name, for note the note",
    )
    app: str | None = Field(default=None, max_length=80, description="for alias only: the app they mean (Y)")

    @model_validator(mode="after")
    def _app_for_alias(self) -> Self:
        if (self.kind == "alias") != (self.app is not None):
            raise ValueError("give app for an alias, and only for an alias")
        return self


class Remember(Tool):
    name = "memory.remember"
    description = (
        "Remember something the user asks to be remembered: what they mean by an app name (alias), "
        "their name, or a note."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = RememberArgs
    grounded = ("text", "app")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(RememberArgs, args)
        if problem := _not_the_users(ctx, a.text, a.app):
            return problem
        text = " ".join(a.text.split())
        if a.kind == "name":
            return Prepared(f"Remember your name: {text}", MemoryItem("name", text))
        if a.kind == "note":
            return Prepared(f"Remember: {text}", MemoryItem("note", text))
        words = normalize(text)
        entry = (
            ctx.resolver.catalog.by_id(choice)
            if choice
            else resolve_app(_users_app(ctx, cast(str, a.app)), ctx)
        )
        if entry is None:
            return ToolOutcome(StepStatus.FAILED, "That app is no longer installed")
        if isinstance(entry, ToolOutcome):
            return entry  # not installed, or several apps could be meant: the user picks
        return Prepared(
            f"Remember: “{words}” means {entry.name}",
            MemoryItem("alias", words, entry.desktop_id, entry.name),
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        item = cast(MemoryItem, prepared.payload)
        ctx.memory.add(item)
        check = Check("it's remembered", _stored(ctx, item), {"kind": item.kind})
        said = {
            "alias": f"I'll remember that “{item.text}” means {item.app_name}",
            "name": f"I'll call you {item.text}",
            "note": f"I'll remember: {item.text}",
        }[item.kind]
        return outcome_from_checks([check], said, "Couldn't save that to memory")


# ---------------------------------------------------------------------------- memory.forget
class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    about: str | None = Field(
        default=None, max_length=120, description="words from the memory, in the user's words"
    )
    all: bool = Field(default=False, description="true only if the user said forget everything")


def _matching(items: list[MemoryItem], about: str) -> list[MemoryItem]:
    wanted = {w for w in normalize(about).split() if w not in CONNECTIVES} - {
        "remember",
        "memory",
        "that",
        "about",
    }
    scored = [(len(wanted & set(normalize(f"{i.text} {i.app_name}").split())), i) for i in items]
    best = max((score for score, _ in scored), default=0)
    return [item for score, item in scored if best and score == best]


class Forget(Tool):
    name = "memory.forget"
    description = "Forget something SONU was asked to remember (by words from it), or everything."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ForgetArgs
    grounded = ("about",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ForgetArgs, args)
        if problem := _not_the_users(ctx, a.about):
            return problem
        items = list(ctx.memory.items)
        if not items:
            return ToolOutcome(StepStatus.FAILED, "I don't remember anything yet")
        if a.all:
            return Prepared(f"Forget everything ({len(items)} things)", items, risk=Risk.MEDIUM)
        if choice is not None:
            chosen = [items[int(choice)]] if choice.isdigit() and int(choice) < len(items) else []
        else:
            chosen = _matching(items, a.about or "")
        if not chosen:
            return ToolOutcome(StepStatus.FAILED, f"I don't remember anything about '{a.about}'")
        if len(chosen) > 1:
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                "several memories match",
                question="Which one should I forget?",
                choices=Choices(
                    "Which one should I forget?",
                    tuple(Choice(item.describe(), str(items.index(item))) for item in chosen[:MAX_CHOICES]),
                ),
            )
        return Prepared(f"Forget: {chosen[0].describe()}", chosen)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        chosen = cast(list[MemoryItem], prepared.payload)
        ctx.memory.remove(chosen)
        gone = not any(_stored(ctx, item) for item in chosen)
        what = chosen[0].describe() if len(chosen) == 1 else f"{len(chosen)} things"
        return outcome_from_checks(
            [Check("it's forgotten", gone, {})], f"Forgot {what}", f"{what} is still remembered"
        )


# ---------------------------------------------------------------------------- memory.list
class ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListMemories(Tool):
    name = "memory.list"
    description = "Tell the user what SONU remembers for them (read-only)."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ListArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        return Prepared("List what I remember", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        items = ctx.memory.items
        if not items:
            return ToolOutcome(StepStatus.DONE, "I don't remember anything yet — say “remember …”")
        # The user's own notes: kept out of the audit log.
        return ToolOutcome(
            StepStatus.DONE, "I remember: " + " · ".join(i.describe() for i in items), private=True
        )


MEMORY_TOOLS: tuple[Tool, ...] = (Remember(), Forget(), ListMemories())
