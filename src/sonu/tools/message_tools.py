"""Unread messages (M7 part 1, D68): "do I have new messages?", "any unread WhatsApp messages?".

SONU reads the count each open chat or mail app shows in its window title — "(3) WhatsApp",
"Telegram (3)", "Inbox (12) - … - Gmail", "• Discord" — and says only the app and the number: never
the rest of the title (names, subjects, an address). An app that isn't open (or only in the tray)
has no window to read: SONU says so instead of counting it as zero. Nothing is opened or sent.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.notifications import NotificationLog
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
MAX_READ = 5
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


# ---------------------------------------------------------------------------- notifications (D69)
NOT_KEPT = (
    "I'm not keeping notifications — to have SONU remember the latest ones (in memory only, while sonud "
    "runs), add [notifications] remember = true to ~/.config/sonu/config.toml"
)


def _log(ctx: ToolContext) -> NotificationLog | ToolOutcome:
    if ctx.notifications is None:
        return ToolOutcome(StepStatus.FAILED, NOT_KEPT)
    return ctx.notifications


class ReadNotificationsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str | None = Field(default=None, max_length=40, description="only this app's, if the user named one")
    which: Literal["latest", "recent"] = Field(
        default="latest", description="latest: 'what was that notification?'; recent: 'read my notifications'"
    )


class ReadNotifications(Tool):
    name = "notifications.read"
    description = (
        "Read the latest notification ('what was that notification?') or the recent ones ('read my "
        "notifications', 'any notifications from WhatsApp?'), as SONU kept them."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ReadNotificationsArgs
    grounded = ("app",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ReadNotificationsArgs, args)
        if a.app and (missing := ungrounded_words(a.app, ctx.user_words or ctx.request_text)):
            return ToolOutcome(
                StepStatus.FAILED, f"The app must be your own word ({', '.join(missing)} isn't)"
            )
        log = _log(ctx)
        if isinstance(log, ToolOutcome):
            return log
        what = "the latest notification" if a.which == "latest" else "the recent notifications"
        return Prepared(f"Read {what}{f' from {a.app}' if a.app else ''}", (log, a))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        log, a = cast("tuple[NotificationLog, ReadNotificationsArgs]", prepared.payload)
        now = time.time()
        items = log.recent(now)
        if a.app:
            wanted = set(normalize(a.app).split()) - {"the", "my", "app"}
            items = [n for n in items if wanted & set(normalize(n.app).split())]
        read = (Check("read the notifications SONU kept", True),)
        if not items:
            minutes = log.keep_s // 60
            source = f" from {a.app}" if a.app else ""
            return ToolOutcome(
                StepStatus.DONE, f"No notifications{source} in the last {minutes} minutes", read
            )
        shown = items[:1] if a.which == "latest" else items[:MAX_READ]
        text = "; ".join(n.describe(now) for n in shown)
        more = len(items) - len(shown)
        if more and a.which == "recent":
            text += f" (and {more} older)"
        # What apps and people wrote: shown and said to the user, never given to a model or the audit log.
        return ToolOutcome(StepStatus.DONE, text, read, private=True)


class ForgetNotificationsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForgetNotifications(Tool):
    name = "notifications.forget"
    description = "Forget the notifications SONU kept ('forget my notifications'). The shell's own stay."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ForgetNotificationsArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        log = _log(ctx)
        return log if isinstance(log, ToolOutcome) else Prepared("Forget the notifications SONU kept", log)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        log = cast(NotificationLog, prepared.payload)
        count = log.forget()
        gone = Check("none kept now", not log.recent())
        return ToolOutcome(
            StepStatus.DONE if gone.passed else StepStatus.FAILED,
            f"Forgot {count} notification{'s' if count != 1 else ''}",
            (gone,),
        )


MESSAGE_TOOLS: tuple[Tool, ...] = (UnreadMessages(), ReadNotifications(), ForgetNotifications())
