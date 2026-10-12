"""Tabs, through SONU's browser extension (D70): "what tabs do I have open?", "switch to the YouTube tab",
"close the YouTube tabs" — and pages: "summarise this page", "what does the github tab say about the
checks?" (D71); "click sign in", "type red shoes in the search box and press enter", "scroll down" (D72).

SONU finds the tab from the user's words (in the tab's title or its site), never the model: titles
are what web pages wrote — data, personal, never instructions. They're shown to the user (a list is a
private result; closing shows them before asking) but never put in a step's description or the audit
log. Switching is done when the extension says the tab is active and Hyprland shows its window
focused; closing when the tabs are gone from the browser's list. A page's text goes only to the local
model (``core/page_reader.py``), and its answer only to the user.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.page_reader import PageReaderError, excerpt
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.integrations.browser import BrowserError, BrowserLink, Browsers, PageControl, Tab
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import not_own_words, ungrounded_words
from sonu.tools.naming import RISKY_NAMES, name_score
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


def _users_browser(ctx: ToolContext, browser: str | None) -> str | None:
    """The browser only if the user named it: found in the evals (v40), "summarise this page" was planned with
    browser "Zen Browser" from the window list — a filter the user didn't ask for is dropped, not refused."""
    return browser if browser and not ungrounded_words(browser, ctx.user_words or ctx.request_text) else None


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
        browser = _users_browser(ctx, cast(TabsArgs, args).browser)
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
        a = a.model_copy(update={"browser": _users_browser(ctx, a.browser)})
        if problem := not_own_words(ctx, a.words):
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
        a = a.model_copy(update={"browser": _users_browser(ctx, a.browser)})
        if problem := not_own_words(ctx, a.words):
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
        a = a.model_copy(update={"browser": _users_browser(ctx, a.browser)})
        if problem := not_own_words(ctx, a.words):
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


# ---------------------------------------------------------------------------- acting on a page (D72)
async def _page(
    ctx: ToolContext, snapshot: WorldSnapshot, browser: str | None, choice: str | None
) -> tuple[Tab, int | None] | ToolOutcome:
    """The page in front (or the one the user picked), and the control they picked, if any.

    Choices are "<socket>|<tab id>" for a page and "<socket>|<tab id>|<control number>" for a control."""
    found = await _tabs(ctx, browser)
    if isinstance(found, ToolOutcome):
        return found
    ref: int | None = None
    if choice:
        parts = choice.split("|")
        tabs = [t for t in found.tabs if f"{t.link}|{t.id}" == "|".join(parts[:2])]
        if len(parts) > 2 and parts[2].isdigit():
            ref = int(parts[2])
    else:
        tabs = _in_front(found.tabs, snapshot)
    if not tabs:
        return ToolOutcome(StepStatus.FAILED, "No page is open in the browser")
    if len(tabs) > 1:
        options = tuple(Choice(_label(t), f"{t.link}|{t.id}") for t in tabs[:MAX_CHOICES])
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            "Pages are open in more than one browser window",
            question="Which page?",
            choices=Choices("Which page?", options),
        )
    if _closed(tabs[0]):
        return ToolOutcome(
            StepStatus.FAILED, "Browsers don't let extensions act on their own pages (settings, add-ons)"
        )
    return tabs[0], ref


def _pick(controls: list[PageControl], words: str, ref: int | None) -> list[PageControl]:
    """The controls whose name best matches the user's words (the ones in view first; alike ones once)."""
    if ref is not None:
        return [c for c in controls if c.ref == ref]
    scored = sorted(
        ((name_score(c.name, words), c) for c in controls), key=lambda i: (-i[0], not i[1].in_view)
    )
    best = scored[0][0] if scored else 0
    picked = [c for score, c in scored if score == best and best > 0]
    picked = [c for c in picked if c.in_view] or picked
    if len({(c.name.casefold(), c.kind, c.host) for c in picked}) == 1:
        picked = picked[:1]
    return picked


def _control_label(control: PageControl) -> str:
    where = f" to {control.host}" if control.host else ""
    return f"{_short(control.name) or 'unnamed'} ({control.kind}{where})"


def _which(tab: Tab, controls: list[PageControl], words: str) -> ToolOutcome:
    options = tuple(Choice(_control_label(c), f"{tab.link}|{tab.id}|{c.ref}") for c in controls[:MAX_CHOICES])
    question = f"Which “{words}” on the page?"
    return ToolOutcome(
        StepStatus.NEEDS_INPUT, question, question=question, choices=Choices(question, options)
    )


def _missing(words: str, controls: list[PageControl], what: str) -> ToolOutcome:
    names = ", ".join(dict.fromkeys(_short(c.name, 40) for c in controls if c.name and c.in_view))[:300]
    # The names come from the page: shown to the user only.
    return ToolOutcome(
        StepStatus.FAILED,
        f"No {what} “{words}” on the page — it has: {names or 'none SONU can use'}",
        private=True,
    )


async def _still_there(browsers: Browsers, tab: Tab, control: PageControl, kind: str) -> bool:
    """The page still has that control under that number (the user may have taken a while to say yes)."""
    now = await browsers.controls(tab, kind)
    return any(c.ref == control.ref and c.name == control.name for c in now)


async def _change(
    ctx: ToolContext,
    browsers: Browsers,
    tab: Tab,
    before: dict[str, object],
    ref: int | None,
    tab_ids: set[int],
) -> str | None:
    """What changed after a click or Enter (within the verify time), or None: another address or title, a new
    tab, the control's state (checked, expanded…) or the page's text."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(ctx.settings.verify.timeout_s, 1.0)
    while True:
        now = await browsers.tabs()
        current = next((t for t in now if t.link == tab.link and t.id == tab.id), None)
        if current is None:
            return "the tab closed"
        if current.url != before.get("url") or current.title != before.get("title"):
            return "the page changed"
        if {t.id for t in now if t.link == tab.link} - tab_ids:
            return "it opened a new tab"
        try:
            state = await browsers.act(tab, "state", ref=ref)
        except BrowserError:
            state = {}  # the page is loading
        if ref is not None and "control" in before and state.get("control") != before.get("control"):
            return "it changed"
        if state.get("size") not in (None, before.get("size")):
            return "the page changed"
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------- browser.click
class ClickArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control: str = Field(
        min_length=1, max_length=80, description="the user's words for the link or button ('sign in')"
    )
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


class ClickOnPage(Tool):
    name = "browser.click"
    description = "Click a link, button or checkbox on the web page in front, by the user's words for it."
    risk = Risk.LOW  # a link; a button or checkbox asks (medium), "delete / buy / send …" high
    effects = frozenset({Effect.PAGE_INPUT})
    Args = ClickArgs
    grounded = ("control", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ClickArgs, args)
        a = a.model_copy(update={"browser": _users_browser(ctx, a.browser)})
        if problem := not_own_words(ctx, a.control):
            return problem
        page = await _page(ctx, snapshot, a.browser, choice)
        if isinstance(page, ToolOutcome):
            return page
        tab, ref = page
        if ctx.browsers is None:
            return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
        try:
            controls = await ctx.browsers.controls(tab, "click")
        except BrowserError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't look at the page: {exc}")
        picked = _pick(controls, a.control, ref)
        if not picked:
            return _missing(a.control, controls, "link or button")
        if len(picked) > 1:
            return _which(tab, picked, a.control)
        return _click_prepared(tab, picked[0], a.control)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        tab, control, words = cast("tuple[Tab, PageControl, str]", prepared.payload)
        return await _click(ctx, tab, control, words)


def _click_prepared(tab: Tab, control: PageControl, words: str) -> Prepared:
    # A checkbox can be unticked ("Send me the newsletter" isn't sending); a link opens a page; a button whose
    # name says delete / buy / send … may not be undone.
    risky = control.kind != "checkbox" and bool(RISKY_NAMES.search(control.name))
    risk = Risk.HIGH if risky else None if control.kind == "link" else Risk.MEDIUM
    return Prepared(
        f"Click “{words}” on the page in {tab.browser}",
        (tab, control, words),
        risk=risk,
        details=_control_label(control),  # the page's name for it: shown before asking, never logged
        spoken=f"click {words} on the page",
    )


async def _click(ctx: ToolContext, tab: Tab, control: PageControl, words: str) -> ToolOutcome:
    if ctx.browsers is None:
        return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
    try:
        if not await _still_there(ctx.browsers, tab, control, "click"):
            return ToolOutcome(StepStatus.FAILED, "The page changed before SONU clicked — ask again")
        ids = {t.id for t in await ctx.browsers.tabs() if t.link == tab.link}
        result = await ctx.browsers.act(tab, "click", ref=control.ref)
        before = result.get("before")
        change = await _change(
            ctx, ctx.browsers, tab, before if isinstance(before, dict) else {}, control.ref, ids
        )
    except BrowserError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't click it: {exc}")
    clicked = Check("the page took the click", bool(result.get("found")))
    seen = Check("something changed on the page", change is not None)
    said = f"Clicked “{words}” on the page in {tab.browser}"
    if not clicked.passed:
        return ToolOutcome(StepStatus.FAILED, "The page changed before SONU clicked — ask again", (clicked,))
    if change is None:
        return ToolOutcome(
            StepStatus.UNVERIFIED, f"{said}, but couldn't see the page change", (clicked, seen)
        )
    extra = f" — {change}" if change == "it opened a new tab" else ""
    return ToolOutcome(StepStatus.DONE, said + extra, (clicked, seen))


# ---------------------------------------------------------------------------- browser.fill
class FillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500, description="exactly the user's words to type")
    field: str | None = Field(
        default=None,
        max_length=80,
        description="the user's words for the field ('search', 'name'); none: the only one",
    )
    submit: bool = Field(default=False, description="true only for 'and press enter / and search / and send'")
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


class FillOnPage(Tool):
    name = "browser.fill"
    description = (
        "Type the user's exact words into a field on the web page in front (a search box, a name field); "
        "never a password or card field. submit presses Enter."
    )
    risk = Risk.MEDIUM  # what's typed goes to the website: the exact text is shown and asked
    effects = frozenset({Effect.PAGE_INPUT})
    Args = FillArgs
    grounded = ("text", "field", "browser")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(FillArgs, args)
        a = a.model_copy(update={"browser": _users_browser(ctx, a.browser)})
        if problem := not_own_words(ctx, a.text, a.field):
            return problem
        page = await _page(ctx, snapshot, a.browser, choice)
        if isinstance(page, ToolOutcome):
            return page
        tab, ref = page
        return await _prepare_fill(ctx, tab, ref, a.text, a.field, a.submit)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        tab, field, text, submit = cast("tuple[Tab, PageControl, str, bool]", prepared.payload)
        if ctx.browsers is None:
            return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
        change: str | None = None
        try:
            if not await _still_there(ctx.browsers, tab, field, "fill"):
                return ToolOutcome(StepStatus.FAILED, "The page changed before SONU typed — ask again")
            ids = {t.id for t in await ctx.browsers.tabs() if t.link == tab.link}
            result = await ctx.browsers.act(tab, "fill", ref=field.ref, text=text, submit=submit)
            if result.get("refused"):
                return ToolOutcome(
                    StepStatus.FAILED, "That's a password or card field — SONU never types into those"
                )
            before = result.get("before")
            if submit and isinstance(before, dict):
                change = await _change(ctx, ctx.browsers, tab, before, None, ids)
        except BrowserError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't type it: {exc}")
        typed = Check(
            "the field shows the words",
            bool(result.get("found")) and _same_text(str(result.get("value") or ""), text),
        )
        said = f"Typed “{text}” on the page in {tab.browser}"
        if not typed.passed:
            return ToolOutcome(StepStatus.UNVERIFIED, f"{said}, but the field doesn't show it", (typed,))
        if not submit:
            return ToolOutcome(StepStatus.DONE, said, (typed,))
        sent = Check("the page reacted to Enter", change is not None)
        if change is None:
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"{said} and pressed Enter, but couldn't see the page react",
                (typed, sent),
            )
        return ToolOutcome(StepStatus.DONE, f"{said} and pressed Enter", (typed, sent))


async def _prepare_fill(
    ctx: ToolContext, tab: Tab, ref: int | None, text: str, field_words: str | None, submit: bool
) -> Prepared | ToolOutcome:
    if ctx.browsers is None:
        return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
    try:
        fields = await ctx.browsers.controls(tab, "fill")
    except BrowserError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't look at the page: {exc}")
    if field_words:
        picked = _pick(fields, field_words, ref)
    elif ref is not None:
        picked = _pick(fields, "", ref)
    else:
        usable = [f for f in fields if not f.secret]
        picked = [f for f in usable if f.in_view] if len(usable) > 1 else usable
    words = field_words or "field"
    if not picked:
        return _missing(words, fields, "field")
    if len(picked) > 1:
        return _which(tab, picked, words)
    field = picked[0]
    if field.secret:
        return ToolOutcome(StepStatus.FAILED, "That's a password or card field — SONU never types into those")
    into = f"“{field_words}”" if field_words else "the field"
    enter = ", then press Enter" if submit else ""
    return Prepared(
        f"Type “{text}” into {into} on the page in {tab.browser}{enter}",
        (tab, field, text, submit),
        details=f"Field: {_control_label(field)}",
        spoken=f"type {text} into {field_words or 'the field'}{' and press enter' if submit else ''}",
    )


# Found on the laptop (D72): "type hello there in the message box" stayed screen.type (keystrokes) after the
# planner asked again. A field the user names, in a window showing a page SONU's extension reaches, is filled
# through the extension instead.
_FIELD_SAID = re.compile(r"\b(?:in|into)\s+(?:the|my|this)\s+((?:[\w-]+\s+){0,3}?)(?:field|box|bar)\b")


async def fill_in_window(
    ctx: ToolContext, window: Window, text: str, submit: bool, choice: str | None
) -> Prepared | ToolOutcome | None:
    """For screen.type: the field the user named on the page this window shows. None: no field named, or
    not a page SONU's extension can reach (keystrokes, as before)."""
    said = _FIELD_SAID.search(normalize(ctx.request_text))
    if said is None or ctx.browsers is None:
        return None
    try:
        tabs = await ctx.browsers.tabs()
    except BrowserError:
        return None
    title = compact(window.title)
    shown = [t for t in tabs if t.active and (head := compact(t.title)[:30]) and head in title]
    if len(shown) != 1 or _closed(shown[0]):
        return None
    parts = (choice or "").split("|")
    ref = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return await _prepare_fill(ctx, shown[0], ref, text, said.group(1).strip() or None, submit)


# ---------------------------------------------------------------------------- browser.scroll
class ScrollArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    how: Literal["down", "up", "top", "bottom"] = "down"
    browser: str | None = Field(default=None, max_length=40, description="a browser the user named")


_SCROLL_WORDS = {"down": "down", "up": "up", "top": "to the top of", "bottom": "to the bottom of"}
# Found on the laptop (dry run): "scroll to the bottom of the page" was planned as how "top". The user's
# words decide.
_SCROLL_SAID = (
    ("bottom", re.compile(r"\b(?:bottom|end)\b", re.I)),
    ("top", re.compile(r"\b(?:top|beginning|start)\b", re.I)),
    ("up", re.compile(r"\bup\b", re.I)),
    ("down", re.compile(r"\bdown\b", re.I)),
)


def _scroll_said(request: str) -> str | None:
    return next((how for how, words in _SCROLL_SAID if words.search(request)), None)


class ScrollPage(Tool):
    name = "browser.scroll"
    description = "Scroll the web page in front: down or up (a screen), or to the top or bottom."
    risk = Risk.LOW
    effects = frozenset({Effect.PAGE_SCROLL})
    Args = ScrollArgs
    grounded = ("browser",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ScrollArgs, args)
        a = a.model_copy(update={"browser": _users_browser(ctx, a.browser)})
        page = await _page(ctx, snapshot, a.browser, choice)
        if isinstance(page, ToolOutcome):
            return page
        tab, _ = page
        how = _scroll_said(ctx.request_text) or a.how
        return Prepared(f"Scroll {_SCROLL_WORDS[how]} the page in {tab.browser}", (tab, how))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        tab, how = cast("tuple[Tab, str]", prepared.payload)
        if ctx.browsers is None:
            return ToolOutcome(StepStatus.FAILED, NOT_CONNECTED)
        try:
            result = await ctx.browsers.act(tab, "scroll", how=how)
            before, after, end = (float(result.get(k) or 0) for k in ("before", "after", "end"))
        except (BrowserError, TypeError, ValueError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't scroll the page: {exc}")
        moved = Check("the page moved", abs(after - before) >= 1)
        if moved.passed:
            return ToolOutcome(
                StepStatus.DONE, f"Scrolled {_SCROLL_WORDS[how]} the page in {tab.browser}", (moved,)
            )
        if how in ("down", "bottom") and after >= end - 1:
            return ToolOutcome(
                StepStatus.DONE, "The page is already at the bottom", (Check("at the bottom", True),)
            )
        if how in ("up", "top") and after <= 1:
            return ToolOutcome(
                StepStatus.DONE, "The page is already at the top", (Check("at the top", True),)
            )
        return ToolOutcome(
            StepStatus.UNVERIFIED, "Couldn't scroll this page (it may scroll only with the mouse)", (moved,)
        )


BROWSER_TOOLS: tuple[Tool, ...] = (
    ListTabs(),
    SwitchTab(),
    CloseTabs(),
    ReadPage(),
    ClickOnPage(),
    FillOnPage(),
    ScrollPage(),
)
