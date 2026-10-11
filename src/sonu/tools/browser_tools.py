"""Tabs, through SONU's browser extension (D70): "what tabs do I have open?", "switch to the YouTube tab",
"close the YouTube tabs" — and pages (D71): "summarise this page", "what does the github tab say about
the checks?".

SONU finds the tab from the user's words (in the tab's title or its site), never the model: titles
are what web pages wrote — data, personal, never instructions. They're shown to the user (a list is a
private result; closing shows them before asking) but never put in a step's description or the audit
log. Switching is done when the extension says the tab is active and Hyprland shows its window
focused; closing when the tabs are gone from the browser's list. A page's text goes only to the local
model (``core/page_reader.py``), and its answer only to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.page_reader import PageReaderError, excerpt
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.integrations.browser import BrowserError, BrowserLink, Tab
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.tools.web_tools import compact
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_CHOICES = 6
MAX_LISTED = 10
NOT_CONNECTED = (
    "No browser has SONU's extension connected — run `sonu browser install`, add the extension, and keep "
    "the browser open"
)
_FILLER = {
    "the",
    "a",
    "my",
    "tab",
    "tabs",
    "in",
    "on",
    "with",
    "that",
    "which",
    "is",
    "open",
    "page",
    "all",
    "of",
}
# "Summarise the selected text": the request decides, not the model.
_SELECTED = re.compile(r"\b(?:selected|selection|highlighted)\b", re.I)
# Pages browsers keep every extension out of: their own settings, add-on stores, other extensions.
_CLOSED_SCHEMES = ("about:", "chrome:", "edge:", "brave:", "vivaldi:", "view-source:", "moz-extension:")
_CLOSED_HOSTS = {"chromewebstore.google.com", "chrome.google.com", "addons.mozilla.org"}


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(tab: Tab) -> str:
    return f"{_short(tab.title) or tab.host or 'untitled'} — {tab.host or tab.browser}"


def matching(tabs: list[Tab], words: str) -> list[Tab]:
    """The tabs whose title or site has every one of the user's words ("youtube", "github pull")."""
    wanted = [compact(w) for w in normalize(words).split() if w not in _FILLER and compact(w)]
    if not wanted:
        return []
    return [t for t in tabs if all(w in compact(t.title) or w in compact(t.host) for w in wanted)]


@dataclass(frozen=True, slots=True)
class _Found:
    links: list[BrowserLink]
    tabs: list[Tab]


async def _tabs(ctx: ToolContext, browser: str | None) -> _Found | ToolOutcome:
    if ctx.browsers is None:
        return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
    try:
        links = await ctx.browsers.links()
        if browser:
            wanted = compact(browser)
            links = [
                link for link in links if wanted in compact(link.browser) or compact(link.browser) in wanted
            ]
        if not links:
            return ToolOutcome(
                StepStatus.FAILED,
                f"{browser} doesn't have SONU's extension connected" if browser else NOT_CONNECTED,
            )
        return _Found(links, await ctx.browsers.tabs(links))
    except BrowserError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't ask the browser: {exc}")


def _own_words(ctx: ToolContext, *values: str | None) -> ToolOutcome | None:
    for value in values:
        if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
            return ToolOutcome(StepStatus.FAILED, f"That must be your own words ({', '.join(missing)} isn't)")
    return None


def _choices(tabs: list[Tab], words: str) -> ToolOutcome:
    options = tuple(Choice(_label(t), f"{t.link}|{t.id}") for t in tabs[:MAX_CHOICES])
    return ToolOutcome(
        StepStatus.NEEDS_INPUT,
        f"{len(tabs)} tabs match '{words}'",
        question=f"Which '{words}' tab?",
        choices=Choices(f"Which '{words}' tab?", options),
    )


# ---------------------------------------------------------------------------- browser.tabs
class TabsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


class ListTabs(Tool):
    name = "browser.tabs"
    description = "The tabs open in the browser(s) with SONU's extension ('what tabs do I have open?')."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.DESKTOP_READ})
    Args = TabsArgs
    grounded = ("browser",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        browser = cast(TabsArgs, args).browser
        if problem := _own_words(ctx, browser):
            return problem
        return Prepared(f"List the tabs{f' in {browser}' if browser else ''}", browser)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        found = await _tabs(ctx, cast("str | None", prepared.payload))
        if isinstance(found, ToolOutcome):
            return found
        parts = []
        for link in found.links:
            tabs = [t for t in found.tabs if t.link == link.path]
            listed = "; ".join(_label(t) + (" (this one)" if t.active else "") for t in tabs[:MAX_LISTED])
            more = f"; and {len(tabs) - MAX_LISTED} more" if len(tabs) > MAX_LISTED else ""
            parts.append(f"{link.browser}: {len(tabs)} tab{'s' if len(tabs) != 1 else ''} — {listed}{more}")
        # Titles are what web pages wrote: shown to the user, never given to a model or the audit log.
        return ToolOutcome(
            StepStatus.DONE, ". ".join(parts), (Check("the browser listed its tabs", True),), private=True
        )


# ---------------------------------------------------------------------------- browser.tab
class SwitchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: str = Field(min_length=1, max_length=80, description="the user's words for the tab ('youtube')")
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


def _window_showing(tab: Tab, snapshot: WorldSnapshot) -> Window | None:
    title = compact(tab.title)[:30]
    return next((w for w in snapshot.windows if title and title in compact(w.title)), None)


class SwitchTab(Tool):
    name = "browser.tab"
    description = "Switch to an open browser tab by the user's words for it ('switch to the YouTube tab')."
    risk = Risk.LOW
    effects = frozenset({Effect.WINDOW_FOCUS})
    Args = SwitchArgs
    grounded = ("words", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SwitchArgs, args)
        if problem := _own_words(ctx, a.words, a.browser):
            return problem
        found = await _tabs(ctx, a.browser)
        if isinstance(found, ToolOutcome):
            return found
        tabs = (
            [t for t in found.tabs if f"{t.link}|{t.id}" == choice]
            if choice
            else matching(found.tabs, a.words)
        )
        if not tabs:
            return ToolOutcome(
                StepStatus.FAILED, f"No open tab matches '{a.words}' ({len(found.tabs)} tabs are open)"
            )
        if len(tabs) > 1:
            return _choices(tabs, a.words)
        return Prepared(f"Switch to the tab matching '{a.words}' in {tabs[0].browser}", (tabs[0], a.words))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        tab, words = cast("tuple[Tab, str]", prepared.payload)
        if ctx.browsers is None:
            return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
        try:
            await ctx.browsers.activate(tab)
            now = {(t.link, t.id): t for t in await ctx.browsers.tabs()}
        except BrowserError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't switch the tab: {exc}")
        current = now.get((tab.link, tab.id))
        active = Check("the browser says the tab is active", current is not None and current.active)
        # Wayland doesn't let a browser take the focus itself: SONU focuses the window that shows the tab.
        window, _ = await ctx.desktop.wait_for(
            lambda s: _window_showing(current or tab, s), timeout_s=ctx.settings.verify.timeout_s
        )
        if window is not None:
            try:
                await ctx.desktop.focus(window)
            except HyprlandError:
                window = None
        snapshot = await ctx.desktop.snapshot()
        focused = window is not None and snapshot.active_address == window.address
        shown = Check("its window is focused", focused)
        said = f"Switched to the tab matching '{words}' in {tab.browser}"
        if active.passed and not focused:
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"{said}, but I couldn't see its window come to the front",
                (active, shown),
            )
        if not active.passed:
            return ToolOutcome(StepStatus.FAILED, f"{tab.browser} didn't switch to the tab", (active, shown))
        return ToolOutcome(
            StepStatus.DONE, said, (active, shown), data={"windows": [window.address]} if window else {}
        )


# ---------------------------------------------------------------------------- browser.close_tabs
class CloseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: str = Field(min_length=1, max_length=80, description="the user's words for the tab(s) ('youtube')")
    all: bool = Field(default=False, description="true only if the user said all / every tab matching")
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


class CloseTabs(Tool):
    name = "browser.close_tabs"
    description = "Close browser tabs by the user's words for them ('close the YouTube tab'); asks first."
    risk = Risk.MEDIUM  # a closed tab's page (a form, a video's place) may be lost: shown and asked
    effects = frozenset({Effect.WINDOW_CLOSE})
    Args = CloseArgs
    grounded = ("words", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(CloseArgs, args)
        if problem := _own_words(ctx, a.words, a.browser):
            return problem
        found = await _tabs(ctx, a.browser)
        if isinstance(found, ToolOutcome):
            return found
        tabs = (
            [t for t in found.tabs if f"{t.link}|{t.id}" == choice]
            if choice
            else matching(found.tabs, a.words)
        )
        if not tabs:
            return ToolOutcome(
                StepStatus.FAILED, f"No open tab matches '{a.words}' ({len(found.tabs)} tabs are open)"
            )
        if len(tabs) > 1 and not a.all:
            return _choices(tabs, a.words)
        count = f"{len(tabs)} tabs" if len(tabs) > 1 else "the tab"
        return Prepared(
            f"Close {count} matching '{a.words}'",
            (tabs, a.words),
            details="\n".join(_label(t) for t in tabs),  # the titles: shown before asking, never logged
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        tabs, words = cast("tuple[list[Tab], str]", prepared.payload)
        if ctx.browsers is None:
            return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
        try:
            await ctx.browsers.close(tabs)
            left = {(t.link, t.id) for t in await ctx.browsers.tabs()}
        except BrowserError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't close the tabs: {exc}")
        gone = Check("the tabs are gone from the browser", not any((t.link, t.id) in left for t in tabs))
        count = f"{len(tabs)} tabs" if len(tabs) > 1 else "the tab"
        if not gone.passed:
            return ToolOutcome(StepStatus.FAILED, f"The browser didn't close {count}", (gone,))
        return ToolOutcome(StepStatus.DONE, f"Closed {count} matching '{words}'", (gone,))


# ---------------------------------------------------------------------------- browser.read
class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1, max_length=300, description="the user's question about the page ('summarise it')"
    )
    words: str | None = Field(
        default=None,
        max_length=80,
        description="the user's words for another tab ('the github tab'); none: this page",
    )
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


# Words that mean the page in front or what's selected on it, not a tab by name ("this page", "the
# selected text").
_THIS = {
    "this",
    "current",
    "here",
    "front",
    "article",
    "website",
    "site",
    "one",
    "it",
    "webpage",
    "web",
    "selected",
    "selection",
    "highlighted",
    "text",
}


def _names_a_tab(words: str) -> bool:
    return any(w not in _FILLER and w not in _THIS for w in normalize(words).split())


def _tab_said(request: str) -> str | None:
    """The tab the user's own words name: "… in the release notes tab" → "release notes"."""
    words = normalize(request).split()
    for end, word in enumerate(words):
        if word not in ("tab", "tabs"):
            continue
        start = end - 1
        while start >= 0 and words[start] not in ("the", "my", "that", "this") and end - start <= 5:
            start -= 1
        if (
            start >= 0
            and words[start] in ("the", "my")
            and _names_a_tab(name := " ".join(words[start + 1 : end]))
        ):
            return name
    return None


def _closed(tab: Tab) -> bool:
    return (
        tab.url.startswith(_CLOSED_SCHEMES)
        or tab.url.startswith("chrome-extension:")
        or tab.host in _CLOSED_HOSTS
    )


def _in_front(tabs: list[Tab], snapshot: WorldSnapshot) -> list[Tab]:
    """The page the user means by "this page": the tab the focused window shows; else the one open tab
    in front; else every browser window's (the user picks)."""
    shown = [t for t in tabs if t.active]
    focused = snapshot.active_window
    if focused is not None:
        title = compact(focused.title)
        for tab in shown:
            if (head := compact(tab.title)[:30]) and head in title:
                return [tab]
    return shown


class ReadPage(Tool):
    name = "browser.read"
    description = (
        "Answer the user's question about a web page from its text, with the local model: 'summarise this "
        "page', 'what does the article say about X?', or a tab they name."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.DESKTOP_READ})
    Args = ReadArgs
    grounded = ("words", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ReadArgs, args)
        if problem := _own_words(ctx, a.words, a.browser):
            return problem
        if ctx.pages is None:
            return ToolOutcome(StepStatus.FAILED, "There's no local model to read the page with")
        found = await _tabs(ctx, a.browser)
        if isinstance(found, ToolOutcome):
            return found
        # Found on the laptop: "explain the selected text in the release notes tab" → words "selected text"
        # (and "release notes" the time before). A tab the user's words name decides.
        words = _tab_said(ctx.request_text) or (a.words if a.words and _names_a_tab(a.words) else None)
        if choice:
            tabs = [t for t in found.tabs if f"{t.link}|{t.id}" == choice]
        elif words:
            tabs = matching(found.tabs, words)
            if not tabs:
                return ToolOutcome(
                    StepStatus.FAILED, f"No open tab matches '{words}' ({len(found.tabs)} tabs are open)"
                )
        else:
            tabs = _in_front(found.tabs, snapshot)
        if not tabs:
            return ToolOutcome(StepStatus.FAILED, "No page is open in the browser")
        if len(tabs) > 1:
            return _choices(tabs, words or "this page")
        tab = tabs[0]
        if _closed(tab):
            return ToolOutcome(
                StepStatus.FAILED, "Browsers don't let extensions read their own pages (settings, add-ons)"
            )
        selected = bool(_SELECTED.search(ctx.request_text))
        what = "the selected text" if selected else "the page"
        where = f"in the tab matching '{words}'" if words else "in front"
        return Prepared(f"Read {what} {where} in {tab.browser}", (tab, a.question, selected))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        tab, question, selected = cast("tuple[Tab, str, bool]", prepared.payload)
        if ctx.browsers is None or ctx.pages is None:
            return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
        try:
            ctx.pages.check_where()
            page = await ctx.browsers.page(tab)
        except PageReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the page: {exc}")
        except BrowserError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't get the page from {tab.browser}: {exc}")
        if selected and not page.selection:
            return ToolOutcome(StepStatus.FAILED, "Nothing is selected on that page")
        text, partial = (page.selection, False) if selected else excerpt(page, ctx.pages.max_text)
        if not text.strip():
            return ToolOutcome(
                StepStatus.FAILED, "That page has no text SONU can read (a video, a picture or a PDF?)"
            )
        try:
            answer = await ctx.pages.read(text, question, partial=partial, selection=selected)
        except PageReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the page: {exc}")
        if partial:
            answer += " (SONU read the start of a long page.)"
        # The page's words, through the local model: for the user's eyes and ears, never for SONU's planner,
        # reply writer or audit log.
        return ToolOutcome(
            StepStatus.DONE, answer, (Check("the local model read the page's text", True),), private=True
        )


BROWSER_TOOLS: tuple[Tool, ...] = (ListTabs(), SwitchTab(), CloseTabs(), ReadPage())
