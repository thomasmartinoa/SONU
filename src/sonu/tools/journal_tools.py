""" "What was I doing yesterday?" — the activity journal (see ``sonu.core.journal``, D6).

journal.read sums up the windows that had the focus in a part of the day (or when something the user
names was last open); journal.recording pauses or resumes it; journal.forget deletes a stretch of it.
What the journal holds is window titles — personal content, written by apps and web pages — so a summary
is a private result: kept out of the audit log and the model, read out by SONU itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.journal import Journal, Span, title_key, title_text
from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import normalize
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome, outcome_from_checks
from sonu.tools.context import ToolContext
from sonu.tools.grounding import CONNECTIVES
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

When = Literal[
    "last_hour", "today", "this_morning", "this_afternoon", "this_evening", "yesterday", "this_week"
]
MAX_APPS = 4
MAX_TITLES = 2
MIN_S = 60.0  # less than a minute in an app isn't worth saying
_SUFFIX = re.compile(r"\s+[-—–|·]\s+([^-—–|·]+)$")
_NOT_ABOUT = {
    "i",
    "was",
    "were",
    "am",
    "doing",
    "working",
    "last",
    "time",
    "when",
    "did",
    "use",
    "used",
    "using",
}


def _off(ctx: ToolContext) -> ToolOutcome | None:
    if ctx.journal is None or not ctx.settings.journal.enabled:
        return ToolOutcome(
            StepStatus.FAILED, "The activity journal is off ([journal] enabled = false in SONU's config)"
        )
    return None


def period(when: When, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, how to say it) — local time."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    parts = {"this_morning": (5, 12, "this morning"), "this_afternoon": (12, 17, "this afternoon")}
    parts["this_evening"] = (17, 24, "this evening")
    if when == "last_hour":
        return now - timedelta(hours=1), now, "in the last hour"
    if when == "yesterday":
        return midnight - timedelta(days=1), midnight, "yesterday"
    if when == "this_week":
        return midnight - timedelta(days=6), now, "in the last 7 days"
    if when in parts:
        start, end, said = parts[when]
        return midnight + timedelta(hours=start), min(now, midnight + timedelta(hours=end)), said
    return midnight, now, "today"


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "less than a minute"
    hours, rest = divmod(minutes, 60)
    words = [f"{hours} hour{'s' * (hours != 1)}"] if hours else []
    if rest:
        words.append(f"{rest} minute{'s' * (rest != 1)}")
    return " ".join(words)


def _day(at: datetime, now: datetime) -> str:
    days = (now.date() - at.date()).days
    return "today" if days == 0 else "yesterday" if days == 1 else at.strftime("%A")


class AppNames:
    """App names for window classes: "zen" → "Zen Browser", a web app's
    "chrome-chatgpt.com__-Default" → "chatgpt.com"."""

    def __init__(self, entries: Iterable[AppEntry]) -> None:
        self.entries = [entry for entry in entries if not entry.no_display]
        self.cache: dict[str, str] = {}

    def __call__(self, window_class: str) -> str:
        if window_class not in self.cache:
            entry = next((e for e in self.entries if window_matches(e, (window_class,))), None)
            if entry is not None:
                self.cache[window_class] = entry.name
            elif window_class.startswith(("chrome-", "brave-", "msedge-")) and "__" in window_class:
                self.cache[window_class] = window_class.split("-", 1)[1].split("__")[0]  # a web app
            else:
                self.cache[window_class] = window_class
        return self.cache[window_class]


def clean_title(title: str, app_class: str, app_name: str) -> str:
    """The title without the app's own name at the end ("engine.py - SONU - Visual Studio Code")."""
    for name in (app_name, app_class):
        exact = re.search(rf"\s+[-—–|·]\s+{re.escape(name)}$", title, re.I) if name else None
        if exact:
            title = title[: exact.start()]
            break
    else:
        match = _SUFFIX.search(title)
        tail = match.group(1).strip().casefold() if match else ""
        if match and tail and (app_name.casefold() in tail or app_class.casefold() in tail):
            title = title[: match.start()]  # "… — Zen Browser Private" / "… - Mozilla Firefox"
    title = title.strip()
    return title if len(title) <= 60 else title[:57].rstrip() + "…"


def summary(spans: list[Span], names: AppNames, said: str) -> str:
    per_app: dict[str, float] = {}
    titles: dict[str, dict[str, float]] = {}
    shown_as: dict[str, str] = {}
    for span in spans:
        name = names(span.app)
        per_app[name] = per_app.get(name, 0.0) + span.seconds
        title = clean_title(span.title, span.app, name)
        if title_key(title) and normalize(title) != normalize(name):
            titles.setdefault(name, {})
            titles[name][title_key(title)] = titles[name].get(title_key(title), 0.0) + span.seconds
            shown_as[title_key(title)] = title_text(title)
    ranked = [(name, s) for name, s in sorted(per_app.items(), key=lambda item: -item[1]) if s >= MIN_S]
    if not ranked:
        return f"Only a few seconds in any window {said}"
    parts = []
    for name, seconds in ranked[:MAX_APPS]:
        top = [t for t, s in sorted(titles.get(name, {}).items(), key=lambda item: -item[1]) if s >= MIN_S]
        shown = ", ".join(f"“{shown_as[t]}”" for t in top[:MAX_TITLES])
        parts.append(f"{name} {duration(seconds)}" + (f" ({shown})" if shown else ""))
    more = len(ranked) - MAX_APPS
    tail = f", and {more} more app{'s' * (more != 1)}" if more > 0 else ""
    total = sum(seconds for _, seconds in ranked)
    return f"{said.capitalize()} ({duration(total)} in windows): " + "; ".join(parts) + tail


def _wanted(about: str) -> list[str]:
    return [w for w in normalize(about).split() if w not in CONNECTIVES and w not in _NOT_ABOUT]


def about_summary(spans: list[Span], names: AppNames, about: str, said: str, now: datetime) -> str | None:
    wanted = _wanted(about)
    found = [
        s for s in spans if all(w in normalize(f"{names(s.app)} {s.app} {s.title}").split() for w in wanted)
    ]
    if not wanted or not found:
        return None
    last = max(found, key=lambda s: s.end)
    when = datetime.fromtimestamp(last.end)
    title = clean_title(last.title, last.app, names(last.app))
    where = names(last.app) + (
        f" (“{title}”)" if title and normalize(title) != normalize(names(last.app)) else ""
    )
    total = sum(s.seconds for s in found)
    return f"“{about}”: {duration(total)} {said}; last {_day(when, now)} at {when:%H:%M} in {where}"


# ---------------------------------------------------------------------------- journal.read
class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: When | None = Field(
        default=None, description="which part of the day; leave out with 'about' to search the whole journal"
    )
    about: str | None = Field(
        default=None,
        max_length=80,
        description="only windows about this, in the user's words (an app, a project, a site)",
    )


class ReadJournal(Tool):
    name = "journal.read"
    description = (
        "Tell the user what they were doing on the computer (from the activity journal): which apps and "
        "windows they used today, this morning, yesterday, …, or when something was last open (read-only)."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ReadArgs
    grounded = ("about",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if problem := _off(ctx):
            return problem
        a = cast(ReadArgs, args)
        if a.about is not None and not _wanted(a.about):
            a = ReadArgs(when=a.when)
        what = f" about “{a.about}”" if a.about else ""
        return Prepared(f"Look in the activity journal{what}", a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(ReadArgs, prepared.payload)
        journal = cast(Journal, ctx.journal)
        now = ctx.now()
        if a.when is None and a.about:
            start = now - timedelta(days=ctx.settings.journal.keep_days)
            end, said = now, f"in the last {ctx.settings.journal.keep_days} days"
        else:
            start, end, said = period(a.when or "today", now)
        spans = journal.spans(start.timestamp(), end.timestamp(), now.timestamp())
        names = AppNames(ctx.resolver.catalog.entries)
        paused = " — the journal is paused (“resume the journal” to record again)" if journal.paused else ""
        if a.about:
            text = about_summary(spans, names, a.about, said, now)
            if text is None:
                return ToolOutcome(
                    StepStatus.DONE, f"Nothing about “{a.about}” in the journal {said}{paused}"
                )
            return ToolOutcome(StepStatus.DONE, text, private=True)
        if not spans:
            first = journal.spans(0, end.timestamp(), now.timestamp())
            since = "" if first else " (it has nothing yet — sonud records the windows you use from now on)"
            return ToolOutcome(StepStatus.DONE, f"Nothing in the journal {said}{since}{paused}")
        return ToolOutcome(StepStatus.DONE, summary(spans, names, said) + paused, private=True)


# ---------------------------------------------------------------------------- journal.recording
class RecordingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool = Field(description="false: pause the journal (stop recording), true: resume it")


class Recording(Tool):
    name = "journal.recording"
    description = "Pause or resume the activity journal (recording which windows the user uses)."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = RecordingArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if problem := _off(ctx):
            return problem
        on = cast(RecordingArgs, args).on
        active = snapshot.active_window
        current = (active.window_class, active.title) if active is not None else ("", "")
        return Prepared("Resume the activity journal" if on else "Pause the activity journal", (on, current))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        on, (app, title) = cast(tuple[bool, tuple[str, str]], prepared.payload)
        journal = cast(Journal, ctx.journal)
        journal.pause(not on, ctx.now().timestamp())
        if on:
            journal.record(app, title, ctx.now().timestamp())  # the window you're in counts from now
        check = Check("the journal is " + ("recording" if on else "paused"), journal.paused != on, {})
        said = (
            "The journal is recording again"
            if on
            else "The journal is paused — nothing is recorded until you resume it"
        )
        return outcome_from_checks([check], said, "The journal's state didn't change")


# ---------------------------------------------------------------------------- journal.forget
class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: Literal["last_hour", "today", "everything"] = Field(description="what to delete from the journal")


class ForgetJournal(Tool):
    name = "journal.forget"
    description = "Delete the activity journal's last hour, today, or everything in it."
    risk = Risk.MEDIUM
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ForgetArgs

    def _since(self, since: str, now: datetime) -> tuple[float, str]:
        if since == "last_hour":
            return (now - timedelta(hours=1)).timestamp(), "the journal's last hour"
        if since == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), "today's journal"
        return 0.0, "the whole journal"

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if problem := _off(ctx):
            return problem
        at, what = self._since(cast(ForgetArgs, args).since, ctx.now())
        count = cast(Journal, ctx.journal).count(at)
        if not count:
            return ToolOutcome(StepStatus.DONE, f"Nothing to delete: {what} is empty")
        return Prepared(
            f"Delete {what} ({count} window{'s' * (count != 1)}) — it can't be undone", (at, what)
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        at, what = cast(tuple[float, str], prepared.payload)
        journal = cast(Journal, ctx.journal)
        journal.forget(at)
        check = Check("nothing left in that time", journal.count(at) == 0, {})
        return outcome_from_checks([check], f"Deleted {what}", f"Couldn't delete {what}")


JOURNAL_TOOLS: tuple[Tool, ...] = (ReadJournal(), Recording(), ForgetJournal())
