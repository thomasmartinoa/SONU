"""History and bookmarks (D73): "find the page about lentil soup I saw yesterday", "what bookmarks do I have
for arch?", "open my arch wiki bookmark".

Read from the browsers' own profile folders (``integrations/browser_data.py``): no extension needed, the
browser may be closed. The page is found from the user's words, never the model's; what's found is the user's
browsing — a list is a private result, and opening a page shows its title and address before asking (from
history) and never puts them in the description or the audit log.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.resolver import normalize
from sonu.integrations.browser_data import Saved, bookmarks, history, profiles
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.tools.journal_tools import When, period
from sonu.tools.naming import name_score
from sonu.tools.web_tools import Target, WebPlan, compact, open_in_browser, prepare_browser
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_LISTED = 8
MAX_CHOICES = 6
# Words that say what the user is looking for, not what the page is about.
_FILLER = {
    "the",
    "a",
    "an",
    "my",
    "that",
    "this",
    "page",
    "pages",
    "site",
    "website",
    "article",
    "about",
    "on",
    "of",
    "for",
    "from",
    "in",
    "i",
    "visited",
    "saw",
    "seen",
    "read",
    "was",
    "bookmark",
    "bookmarks",
    "history",
    "tab",
    "link",
}


def copies_folder() -> Path:
    """Where SONU copies a browser's database to read it (its own 0700 runtime folder; tmpfs)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "sonu" / "copies"


def search_words(words: str | None) -> list[str]:
    return [w for w in normalize(words or "").split() if w not in _FILLER]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _day(when: float, now: datetime) -> str:
    if not when:
        return ""
    at = datetime.fromtimestamp(when)
    days = (now.date() - at.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{at:%a %d %b}"
    return f"{day} {at:%H:%M}"


def _own_words(ctx: ToolContext, *values: str | None) -> ToolOutcome | None:
    for value in values:
        if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
            return ToolOutcome(StepStatus.FAILED, f"That must be your own words ({', '.join(missing)} isn't)")
    return None


def _find(
    ctx: ToolContext,
    kind: Literal["history", "bookmark"],
    words: list[str],
    when: When | None,
    browser: str | None,
) -> list[Saved]:
    """Every profile's pages with all the words (newest first), only web pages."""
    now = ctx.now()
    since, until = (0.0, time.time() + 60) if when is None else (t.timestamp() for t in period(when, now)[:2])
    found: list[Saved] = []
    for profile in profiles(ctx.system.home):
        if browser and compact(browser) not in compact(profile.browser):
            continue
        try:
            if kind == "history":
                found += history(profile, copies_folder(), words, since, until)
            else:
                found += bookmarks(profile, copies_folder(), words)
        except (OSError, sqlite3.Error):
            continue  # a profile SONU can't read (a newer layout): the others still count
    seen: set[str] = set()
    unique = []
    for saved in sorted(found, key=lambda s: -s.when):
        if saved.url.startswith(("https://", "http://")) and saved.url not in seen:
            seen.add(saved.url)
            unique.append(saved)
    return unique


def _label(saved: Saved) -> str:
    return f"{_short(saved.title) or _host(saved.url)} — {_host(saved.url)}"


# ---------------------------------------------------------------------------- browser.history
class HistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: str | None = Field(
        default=None,
        max_length=80,
        description="the user's words for the page ('lentil soup'); none: everything",
    )
    when: When | None = Field(default=None, description="only if the user said a time ('yesterday')")
    kind: Literal["history", "bookmarks"] = "history"
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


class FindSaved(Tool):
    name = "browser.history"
    description = (
        "Search the user's browser history (or bookmarks) by their words and/or a time: 'find the page "
        "about X I saw yesterday', 'my bookmarks for arch'."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.DESKTOP_READ})
    Args = HistoryArgs
    grounded = ("words", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(HistoryArgs, args)
        if problem := _own_words(ctx, a.words, a.browser):
            return problem
        if a.words and ungrounded_words(a.words, ctx.request_text):
            # Found on the laptop: "what pages did I visit today?" right after "find the page about lentil
            # soup" was planned with words "lentil soup". A search uses this request's words only.
            missing = set(ungrounded_words(a.words, ctx.request_text))
            kept = " ".join(w for w in normalize(a.words).split() if w not in missing)
            a = a.model_copy(update={"words": kept or None})
        what = "bookmarks" if a.kind == "bookmarks" else "history"
        about = f" for “{a.words}”" if a.words else ""
        return Prepared(f"Search your {what}{about}", a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(HistoryArgs, prepared.payload)
        kind: Literal["history", "bookmark"] = "bookmark" if a.kind == "bookmarks" else "history"
        when = a.when if a.when or a.words or kind == "bookmark" else "today"
        found = await asyncio.to_thread(_find, ctx, kind, search_words(a.words), when, a.browser)
        noun = "bookmark" if kind == "bookmark" else "page"
        what = f"{noun}s" if kind == "bookmark" else "pages in your history"
        about = f" for “{a.words}”" if a.words else ""
        said = f" ({period(when, ctx.now())[2]})" if when else ""
        checked = (Check(f"the browsers' {kind} was read", True),)
        if not found:
            return ToolOutcome(StepStatus.DONE, f"No {what}{about}{said}", checked)
        now = ctx.now()
        listed = "; ".join(
            _label(s) + (f" ({day})" if kind == "history" and (day := _day(s.when, now)) else "")
            for s in found[:MAX_LISTED]
        )
        more = f"; and {len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
        # The user's browsing: shown to them, never given to a model or the audit log.
        counted = what if len(found) != 1 else what.replace(f"{noun}s", noun, 1)
        return ToolOutcome(
            StepStatus.DONE, f"{len(found)} {counted}{about}{said}: {listed}{more}", checked, private=True
        )


# ---------------------------------------------------------------------------- browser.open_saved
class OpenSavedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: str = Field(min_length=1, max_length=80, description="the user's words for the page ('arch wiki')")
    kind: Literal["bookmark", "history"] | None = Field(
        default=None, description="bookmark or history if the user said which; none: bookmarks first"
    )
    when: When | None = Field(default=None, description="only if the user said a time ('yesterday')")
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


def _key(saved: Saved) -> str:
    return "saved:" + hashlib.sha256(saved.url.encode()).hexdigest()[:16]


def _best(found: list[Saved], words: str) -> list[Saved]:
    """The pages whose title best matches the user's words (any match counts: the address may have them)."""
    scored = sorted(((name_score(s.title, words), s) for s in found), key=lambda i: (-i[0], -i[1].visits))
    best = scored[0][0] if scored else 0
    return [s for score, s in scored if score == best]


class OpenSaved(Tool):
    name = "browser.open_saved"
    description = (
        "Open a page from the user's bookmarks or history by their words for it: 'open my arch wiki "
        "bookmark', 'open that lentil soup page I saw yesterday'."
    )
    risk = Risk.LOW  # a bookmark; a page from history shows its address and asks (medium)
    effects = frozenset({Effect.WEB_OPEN, Effect.APP_LAUNCH})
    Args = OpenSavedArgs
    grounded = ("words", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(OpenSavedArgs, args)
        if problem := _own_words(ctx, a.words, a.browser):
            return problem
        words = search_words(a.words)
        if not words:
            return ToolOutcome(StepStatus.FAILED, "Which page? Say a word from its name")
        said_bookmark = "bookmark" in normalize(ctx.request_text)
        kind = a.kind or ("bookmark" if said_bookmark or a.when is None else "history")
        found = await asyncio.to_thread(_find, ctx, kind, words, a.when if kind == "history" else None, None)
        if not found and a.kind is None and kind == "bookmark" and not said_bookmark:
            kind = "history"
            found = await asyncio.to_thread(_find, ctx, kind, words, a.when, None)
        if choice and choice.startswith("saved:"):
            picked = [s for s in found if _key(s) == choice]
        else:
            picked = _best(found, " ".join(words))
        what = "bookmark" if kind == "bookmark" else "page in your history"
        if not picked:
            return ToolOutcome(StepStatus.FAILED, f"No {what} matches “{a.words}”")
        if len(picked) > 1:
            options = tuple(Choice(_label(s), _key(s)) for s in picked[:MAX_CHOICES])
            question = f"Which “{a.words}”?"
            return ToolOutcome(
                StepStatus.NEEDS_INPUT, question, question=question, choices=Choices(question, options)
            )
        saved = picked[0]
        marker = compact(saved.title)[:30] or compact(_host(saved.url).split(".")[0])
        # The label goes into summaries and the audit log: the user's words, never the page's title.
        target = Target(saved.url, f"your {kind} matching “{a.words}”", marker)
        browser = a.browser or saved.browser
        plan = prepare_browser(target, browser, ctx, None)
        if isinstance(plan, ToolOutcome) and not a.browser:
            plan = prepare_browser(target, None, ctx, None)  # its browser isn't installed any more
        if isinstance(plan, ToolOutcome):
            return plan
        return Prepared(
            f"Open {target.label} in {plan.entry.name}",
            plan,
            # From history: a page the user may not have meant to keep (a sign-in link) — shown and asked.
            risk=Risk.MEDIUM if kind == "history" else None,
            details=f"{_short(saved.title, 100)}\n{_short(saved.url, 200)}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        return await open_in_browser(cast(WebPlan, prepared.payload), ctx)


HISTORY_TOOLS: tuple[Tool, ...] = (FindSaved(), OpenSaved())
