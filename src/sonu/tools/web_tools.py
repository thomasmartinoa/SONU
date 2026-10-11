"""Opening websites and searches in the user's browser.

A link is a way to send data off the machine, so these tools only ever send the user's own words:

- ``web.search`` puts the query into a fixed URL template for a known site (YouTube, GitHub, …)
  or the configured search engine, and every query word must come from the user's request;
- ``web.open`` opens a known site by name or a link/domain the user typed themselves.

The model never builds a URL. Text from window titles or web pages therefore can't choose where
SONU goes or smuggle data out in a link. Results are verified like everything else: the browser's
window title must change to show the site (e.g. "networkchuck - YouTube") before SONU says done.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import quote_plus, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.desktop_tools import chosen_app, resolve_app
from sonu.tools.grounding import CONNECTIVES, ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot


@dataclass(frozen=True, slots=True)
class Site:
    name: str  # shown to the user
    home: str
    search: str  # URL template; {q} is the URL-encoded query
    marker: str  # what the browser's window title shows once the site has loaded
    words: tuple[str, ...]  # how people name it (normalised)


SITES: dict[str, Site] = {
    "youtube": Site(
        "YouTube",
        "https://www.youtube.com/",
        "https://www.youtube.com/results?search_query={q}",
        "YouTube",
        ("youtube", "yt"),
    ),
    "github": Site("GitHub", "https://github.com/", "https://github.com/search?q={q}", "GitHub", ("github",)),
    "wikipedia": Site(
        "Wikipedia",
        "https://en.wikipedia.org/",
        "https://en.wikipedia.org/w/index.php?search={q}",
        "Wikipedia",
        ("wikipedia",),
    ),
    "archwiki": Site(
        "ArchWiki",
        "https://wiki.archlinux.org/",
        "https://wiki.archlinux.org/index.php?search={q}",
        "ArchWiki",
        ("archwiki", "arch wiki"),
    ),
    "aur": Site(
        "AUR", "https://aur.archlinux.org/", "https://aur.archlinux.org/packages?K={q}", "AUR", ("aur",)
    ),
    "reddit": Site(
        "Reddit", "https://www.reddit.com/", "https://www.reddit.com/search/?q={q}", "Reddit", ("reddit",)
    ),
}
ENGINES: dict[str, Site] = {
    "duckduckgo": Site(
        "DuckDuckGo",
        "https://duckduckgo.com/",
        "https://duckduckgo.com/?q={q}",
        "DuckDuckGo",
        ("duckduckgo",),
    ),
    "google": Site(
        "Google", "https://www.google.com/", "https://www.google.com/search?q={q}", "Google", ("google",)
    ),
    "brave": Site(
        "Brave Search",
        "https://search.brave.com/",
        "https://search.brave.com/search?q={q}",
        "Brave",
        ("brave search",),
    ),
    "startpage": Site(
        "Startpage",
        "https://www.startpage.com/",
        "https://www.startpage.com/do/search?q={q}",
        "Startpage",
        ("startpage",),
    ),
}

SearchSite = Literal["web", "youtube", "github", "wikipedia", "archwiki", "aur", "reddit"]

_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# How long an already-running browser gets to react (a window title changes) to a link.
WEB_REACT_S = 8.0
# A search operator the model sometimes adds: "dotfiles site:github.com".
_SITE_OPERATOR = re.compile(r"(?<!\S)site:(\S+)", re.IGNORECASE)


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def site_named(words: str) -> str | None:
    """The key of the one known website these words are exactly ("youtube" → "youtube"), if any."""
    wanted = normalize(words)
    return next((key for key, site in SITES.items() if wanted in (*site.words, normalize(site.name))), None)


def sites_named_in(request: str) -> list[str]:
    return [key for key, site in SITES.items() if _named_in(site, request)]


def _named_in(site: Site, request: str) -> bool:
    text = f" {normalize(request)} "
    return any(f" {word} " in text for word in (*site.words, normalize(site.name)))


def _valid_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        return len(labels) >= 2 and all(_HOST_LABEL.match(label) for label in labels)
    return True


@dataclass(frozen=True, slots=True)
class Target:
    url: str
    label: str  # "YouTube search for 'networkchuck'", "github.com"
    marker: str  # compact text expected in the window title; "" if SONU can't know it


def site_link(text: str, request: str) -> Target | ToolOutcome:
    """A site the user named ("youtube") or a link/domain they typed ("github.com/hyprwm")."""
    key = normalize(text)
    for site in (*SITES.values(), *ENGINES.values()):
        if key in site.words or key == normalize(site.name):
            if not _named_in(site, request):
                return ToolOutcome(StepStatus.FAILED, f"SONU only opens sites you named ({site.name} wasn't)")
            return Target(site.home, site.name, compact(site.marker))

    raw = text.strip()
    url = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return ToolOutcome(StepStatus.FAILED, f"'{raw}' is not a web address")
    if parts.scheme not in ("https", "http"):
        return ToolOutcome(StepStatus.FAILED, f"SONU only opens web links (https://…), not '{parts.scheme}:'")
    if parts.username or parts.password or not host or not _valid_host(host):
        return ToolOutcome(StepStatus.FAILED, f"'{raw}' is not a web address SONU will open")
    typed = raw.removeprefix("https://").removeprefix("http://").rstrip("/")
    if typed.casefold() not in request.casefold():
        # Found in `sonu eval --cloud`: "go to youtube" was planned as "youtube.com". A known site's own
        # address, nothing after it, is the site the user named; anything more must be typed.
        bare = typed.casefold().removeprefix("www.")
        for site in (*SITES.values(), *ENGINES.values()):
            if bare == (urlsplit(site.home).hostname or "").removeprefix("www.") and _named_in(site, request):
                return Target(site.home, site.name, compact(site.marker))
        return ToolOutcome(StepStatus.FAILED, "SONU only opens links you typed yourself")
    label = typed if len(typed) <= 60 else typed[:59] + "…"
    return Target(url, label, compact(_brand(host) if port is None else ""))


def _brand(host: str) -> str:
    """The name a site usually shows in its title: "wiki.archlinux.org" → "archlinux", "bbc.co.uk" → "bbc"."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) < 2:
            return ""
        name = labels[-2]
        if len(name) <= 3 and len(labels) >= 3:  # co.uk, com.au
            name = labels[-3]
        return name
    return ""


# ---------------------------------------------------------------------------- the shared part
@dataclass(frozen=True, slots=True)
class WebPlan:
    entry: AppEntry
    argv: tuple[str, ...]
    target: Target


def prepare_browser(
    target: Target, browser: str | None, ctx: ToolContext, choice: str | None
) -> WebPlan | ToolOutcome:
    entry: AppEntry | ToolOutcome
    if choice is not None:
        entry = chosen_app(choice, ctx)
    elif browser is None and ctx.task_browser is not None:
        entry = ctx.task_browser  # "open chrome on 3 and search …": the browser this request opened
    elif browser is None and len(named := ctx.resolver.browsers_named_in(ctx.request_text)) == 1:
        # Found on real hardware: "play faded on youtube in firefox" was planned without a browser
        # and played in Zen. The browser the user named, in their own words, wins over the default.
        entry = named[0]
    else:
        entry = resolve_app(browser or "browser", ctx)
    if isinstance(entry, ToolOutcome):
        return entry
    if entry.accepts not in ("url", "urls"):
        return ToolOutcome(StepStatus.FAILED, f"{entry.name} can't open web links")
    argv = ctx.launch_argv(entry)
    if argv is None:
        return ToolOutcome(StepStatus.FAILED, f"{entry.name} can't be started")
    return WebPlan(entry, (*argv, target.url), target)


async def open_in_browser(plan: WebPlan, ctx: ToolContext) -> ToolOutcome:
    entry, target = plan.entry, plan.target
    before = await ctx.desktop.snapshot()
    titles = {window.address: window.title for window in before.windows}
    learned = ctx.learned(entry)
    try:
        await ctx.desktop.launch(plan.argv)
    except HyprlandError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Hyprland refused to start {entry.name}: {exc}")

    def changed(snapshot: WorldSnapshot) -> list[Window]:
        return [
            window
            for window in snapshot.windows
            if window_matches(entry, (window.window_class, window.initial_class), learned)
            and titles.get(window.address) != window.title
        ]

    def loaded(snapshot: WorldSnapshot) -> Window | None:
        if not target.marker:
            return next(iter(changed(snapshot)), None)
        return next((w for w in changed(snapshot) if target.marker in compact(w.title)), None)

    running = any(
        window_matches(entry, (window.window_class, window.initial_class), learned)
        for window in before.windows
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    # 1. Did the browser react at all? A running browser answers within seconds; a cold start
    #    (browser not open yet) may take as long as any app launch.
    launch_s = ctx.settings.apps.launch_timeout_s
    react_s = min(WEB_REACT_S, launch_s) if running else launch_s
    reacted, _ = await ctx.desktop.wait_for(
        lambda s: loaded(s) or next(iter(changed(s)), None), timeout_s=react_s
    )
    if reacted is None:
        # Hyprland starts programs without reporting failures, and a browser may open the page in
        # a background tab: SONU can't tell "not opened" from "not visible", so it claims neither.
        return ToolOutcome(
            StepStatus.UNVERIFIED,
            f"Sent {target.label} to {entry.name}, but didn't see it open within {react_s:.0f}s "
            "(it may be in a background tab or another window)",
        )
    # 2. It reacted: give the page time to load (slow connections take well over 10s; found on
    #    real hardware) until its title shows the site.
    remaining = max(0.0, ctx.settings.web.load_timeout_s - (loop.time() - started))
    window, _ = await ctx.desktop.wait_for(loaded, timeout_s=remaining)
    if window is not None and target.marker:
        check = Check(f"{entry.name} shows {target.label}", True, {"title_contains": target.marker})
        return ToolOutcome(StepStatus.DONE, f"Opened {target.label} in {entry.name}", (check,))
    return ToolOutcome(
        StepStatus.UNVERIFIED,
        f"Sent {target.label} to {entry.name}, but couldn't confirm the page loaded",
    )


# ---------------------------------------------------------------------------- web.search
def _without_site_operator(query: str, site: str, request: str) -> tuple[str, str]:
    """ "dotfiles site:github.com" (site "web") → ("dotfiles", "github"), if the user named GitHub.

    Found on real hardware: the model sometimes writes a search-engine operator instead of using
    the site parameter. For a known site the user named, SONU searches that site directly; any
    other operator is left in the query (and then has to pass the grounding check like any word).
    Also, site "web" while the user named exactly one known site ("play faded on youtube") becomes
    that site (found in an eval: the model wrote "Searching YouTube" but left the site out).
    """
    match = _SITE_OPERATOR.search(query)
    if match is None and site == "web":
        # The model left the site out, but the user named exactly one ("… on youtube"): use it.
        named = [key for key, known in SITES.items() if _named_in(known, request)]
        return " ".join(query.split()), named[0] if len(named) == 1 else site
    if match is None or site != "web":
        return " ".join(query.split()), site
    host = match.group(1).lower().removeprefix("www.").split("/")[0]
    for key, known in SITES.items():
        known_host = (urlsplit(known.home).hostname or "").removeprefix("www.")
        if host in (known_host, known_host.split(".", 1)[-1]) and _named_in(known, request):
            rest = (query[: match.start()] + query[match.end() :]).split()
            return " ".join(rest), key
    return " ".join(query.split()), site


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200, description="what to search for, in the user's words")
    site: SearchSite = Field(
        default="web",
        description="where: web (search engine), youtube, github, wikipedia, archwiki, aur or reddit",
    )
    browser: str | None = Field(
        default=None, max_length=80, description="only if the user named a browser, e.g. 'chrome'"
    )


class WebSearch(Tool):
    name = "web.search"
    description = "Search a website or the web in the user's browser ('search youtube for X', 'look up X')."
    risk = Risk.LOW
    effects = frozenset({Effect.WEB_OPEN, Effect.APP_LAUNCH})
    Args = SearchArgs
    grounded = ("query",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SearchArgs, args)
        query, site_key = _without_site_operator(a.query, a.site, ctx.request_text)
        if (
            site_key == "web"
            and len(named := [k for k in sites_named_in(ctx.request_text) if SITES[k].search]) == 1
        ):
            # Found in the evals (v38): "search for username thomasmartinoa in github" was planned as a web
            # search for "username thomasmartinoa github". The one site the user's words name is searched.
            site_key = named[0]
        words = query.split()
        if site_key != "web" and len(words) > 1:  # "… github" at the end of a GitHub search is the site
            site_words = {*SITES[site_key].words, normalize(SITES[site_key].name)}
            if normalize(words[-1]) in site_words:
                query = " ".join(words[:-1])
        if not query:
            return ToolOutcome(StepStatus.FAILED, "There's nothing to search for")
        missing = ungrounded_words(query, ctx.user_words or ctx.request_text)
        if missing:
            return ToolOutcome(
                StepStatus.FAILED,
                f"Won't search for '{query}': {', '.join(missing)} isn't in your request "
                "(SONU only sends your own words to websites)",
            )
        site = ENGINES[ctx.settings.web.search_engine] if site_key == "web" else SITES[site_key]
        url = site.search.format(q=quote_plus(query))
        target = Target(url, f"{site.name} search for '{query}'", compact(site.marker))
        if site_key == "github" and (person := github_user(query, ctx.request_text)):
            # "search for username X in github" searched repositories for "username X".
            url = f"https://github.com/search?q={quote_plus(person)}&type=users"
            target = Target(url, f"GitHub user search for '{person}'", compact(site.marker))
        plan = prepare_browser(target, a.browser, ctx, choice)
        if isinstance(plan, ToolOutcome):
            return plan
        return Prepared(f"Search {site.name} for '{a.query.strip()}' in {plan.entry.name}", plan)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        return await open_in_browser(cast(WebPlan, prepared.payload), ctx)


_LOGIN = r"([a-z0-9][a-z0-9-]{0,38})"  # a GitHub username: one word
_GITHUB_USER = re.compile(
    rf"^(?:the |a )?(?:user ?name|user|account|profile|person)s?(?: (?:named|called|of))? {_LOGIN}$"
    rf"|^{_LOGIN}(?:'s)? (?:user ?name|user|account|profile)$",
    re.IGNORECASE,
)


def github_user(query: str, request: str = "") -> str | None:
    """The name in "username thomasmartinoa", "user named X", "X's profile" — a person, not a repo.
    The model often leaves "username" out of the query, so the user's request is read too."""
    query = " ".join(query.split())
    match = _GITHUB_USER.match(query)
    name = (match.group(1) or match.group(2)) if match else None
    if name is None and re.fullmatch(_LOGIN, query, re.IGNORECASE):
        said = re.search(
            rf"(?:user ?name|user|account|profile|person)s?(?: (?:named|called|of))? {re.escape(query)}\b"
            rf"|\b{re.escape(query)}(?:'s)? (?:user ?name|user|account|profile)\b",
            " ".join(request.split()),
            re.IGNORECASE,
        )
        name = query if said else None
    return name if name and name.lower() not in CONNECTIVES else None


# ---------------------------------------------------------------------------- web.open
class OpenSiteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str = Field(
        min_length=1,
        max_length=300,
        description="the site or link exactly as the user named it: 'youtube', 'github.com', 'https://…'",
    )
    browser: str | None = Field(
        default=None, max_length=80, description="only if the user named a browser, e.g. 'chrome'"
    )


class OpenSite(Tool):
    name = "web.open"
    description = (
        "Open a website the user named (e.g. youtube, github.com, a https:// link) in their browser."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.WEB_OPEN, Effect.APP_LAUNCH})
    Args = OpenSiteArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(OpenSiteArgs, args)
        target = site_link(a.site, ctx.request_text)
        if isinstance(target, ToolOutcome):
            return target
        plan = prepare_browser(target, a.browser, ctx, choice)
        if isinstance(plan, ToolOutcome):
            return plan
        return Prepared(f"Open {target.label} in {plan.entry.name}", plan)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        return await open_in_browser(cast(WebPlan, prepared.payload), ctx)


WEB_TOOLS: tuple[Tool, ...] = (WebSearch(), OpenSite())
