"""Unread messages (M7 part 1, D68): "do I have new messages?", "any unread WhatsApp messages?".

SONU reads the count each open chat or mail app shows in its window title — "(3) WhatsApp",
"Telegram (3)", "Inbox (12) - … - Gmail", "• Discord" — and says only the app and the number: never
the rest of the title (names, subjects, an address). An app that isn't open (or only in the tray)
has no window to read: SONU says so instead of counting it as zero. Nothing is opened or sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.models import Window
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

_CHAT_CATEGORIES = {"InstantMessaging", "Chat", "Email"}
# Chat and mail apps whose desktop files don't say so (WhatsApp Desktop: "Office"; a Gmail web app:
# "WebBrowser"): by a word of their name.
_CHAT_NAMES = {
    "whatsapp",
    "telegram",
    "discord",
    "signal",
    "slack",
    "element",
    "messenger",
    "teams",
    "gmail",
    "outlook",
    "thunderbird",
    "mail",
    "vesktop",
    "webcord",
    "zulip",
}
# Where apps put their count: "(3) WhatsApp" / "(3) Discord | …", "Telegram (3)", "Inbox (12) - …".
_LEADING = re.compile(r"^\s*\((\d{1,5})\)")
_DOT = re.compile(r"^\s*[•●]")  # Discord: unread, no number


def is_chat_app(entry: AppEntry) -> bool:
    if _CHAT_CATEGORIES & set(entry.categories):
        return True
    return bool(_CHAT_NAMES & set(normalize(entry.name).split()))


_MAIL_WORDS = re.compile(r"\b(?:e ?mails?|mails?|inbox)\b")
_MAIL_NAMES = {"gmail", "outlook", "mail", "thunderbird", "evolution", "geary", "proton"}


def is_mail_app(entry: AppEntry) -> bool:
    return "Email" in entry.categories or bool(_MAIL_NAMES & set(normalize(entry.name).split()))


def unread_in(title: str, app_name: str = "") -> int | None:
    """The unread count a chat app shows in its title — first ("(3) WhatsApp"), or right after "Inbox" or
    the app's own name ("Telegram (3)") — -1: unread, no number; None: none shown."""
    names = ["inbox", *(re.escape(word) for word in normalize(app_name).split() if len(word) > 2)]
    after_name = re.compile(rf"^\s*(?:{'|'.join(names)})\s*\((\d{{1,5}})\)", re.IGNORECASE)
    if match := _LEADING.match(title) or after_name.match(title):
        return int(match.group(1))
    return -1 if _DOT.match(title) else None


@dataclass(frozen=True, slots=True)
class _Found:
    entry: AppEntry
    windows: tuple[Window, ...]


def _say(found: _Found) -> str:
    counts = [unread_in(window.title, found.entry.name) for window in found.windows]
    numbers = [c for c in counts if c is not None and c > 0]
    if numbers:
        total = max(numbers)  # one app's windows show the same count
        return f"{found.entry.name}: {total} unread"
    if -1 in counts:
        return f"{found.entry.name}: something unread (no number shown)"
    return f"{found.entry.name}: nothing unread shown"


class UnreadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str | None = Field(default=None, max_length=40, description="one app the user named ('whatsapp')")


class UnreadMessages(Tool):
    name = "messages.unread"
    description = (
        "How many unread messages the open chat and mail apps show (WhatsApp, Telegram, Discord, Gmail…): "
        "'do I have new messages?', 'any unread emails?'. Reads only the counts; sends nothing."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.DESKTOP_READ})
    Args = UnreadArgs
    grounded = ("app",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        named = cast(UnreadArgs, args).app
        if named and (missing := ungrounded_words(named, ctx.user_words or ctx.request_text)):
            return ToolOutcome(
                StepStatus.FAILED, f"The app must be your own word ({', '.join(missing)} isn't)"
            )
        apps = [e for e in ctx.resolver.catalog.entries if not e.no_display and is_chat_app(e)]
        # Found on the laptop (dry run): "how many unread emails do I have?" was planned with no app — the
        # user's words decide that it's mail.
        about_mail = bool(_MAIL_WORDS.search(normalize(named or ctx.request_text)))
        if about_mail:
            apps = [e for e in apps if is_mail_app(e)]
        elif named:
            wanted = set(normalize(named).split()) - {"messages", "message", "chats", "my", "the", "app"}
            apps = [e for e in apps if wanted & set(normalize(e.name).split())]
        if not apps:
            what = f"'{named}'" if named else "any chat or mail app"
            return ToolOutcome(StepStatus.FAILED, f"I don't see {what} installed")
        return Prepared(f"Read the unread counts of {', '.join(e.name for e in apps)}", apps)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        apps = cast("list[AppEntry]", prepared.payload)
        snapshot = await ctx.desktop.snapshot()
        found: list[_Found] = []
        closed: list[str] = []
        for entry in apps:
            learned = ctx.learned(entry)
            windows = tuple(
                w
                for w in snapshot.windows
                if window_matches(entry, (w.window_class, w.initial_class), learned)
            )
            if windows:
                found.append(_Found(entry, windows))
            else:
                closed.append(entry.name)
        parts = [_say(f) for f in found]
        if closed:
            names = ", ".join(closed[:-1]) + f" and {closed[-1]}" if len(closed) > 1 else closed[0]
            verb = "aren't" if len(closed) > 1 else "isn't"
            parts.append(f"{names} {verb} open, so I can't see {'their' if len(closed) > 1 else 'its'} count")
        check = Check("read the open apps' window titles (counts only)", True, {"open": len(found)})
        return ToolOutcome(StepStatus.DONE, "; ".join(parts), (check,))


MESSAGE_TOOLS: tuple[Tool, ...] = (UnreadMessages(),)
