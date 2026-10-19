"""Finding the window(s) a step refers to — deterministically, against the current snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.models import Window
from sonu.tools.context import ToolContext
from sonu.world.snapshot import WorldSnapshot

_PARTS = re.compile(r"\s*(?:[,;]|\band then\b|\bthen\b|\band\b)\s*", re.I)
_ANY_WORKSPACE = re.compile(r"\b(?:on|in|to|at)\s+(?:(?:workspace|work space|ws)\s*)?(\d{1,2})\b", re.I)
_ON_WORKSPACE = re.compile(r"\b(?:on|in|to) workspace (\d{1,2})\b", re.I)
_OPENS = re.compile(r"\b(?:open|launch|start|run)\b", re.I)  # a part that opens something else


def workspace_said(request: str, about: re.Pattern[str]) -> int | None:
    """The workspace the user's words give a step the planner left without one: the number in the part of
    the request about it ("… and claude code in 5 and …" → 5), else the one workspace a part that opens
    nothing else names.

    Found on real hardware: "open browser in 4 and open nvim in 5 and claude code in 5 and …" planned Claude
    Code with no workspace, and only "on workspace 5" was understood — it opened where the owner was.
    """
    named = {
        int(number)
        for part in _PARTS.split(" ".join(request.split()))
        if about.search(part)
        for number in _ANY_WORKSPACE.findall(part)
    }
    if not named:  # "… claude code in sonu, put it on workspace 7" — not "open kitty on workspace 2 and …"
        named = {
            int(number)
            for part in _PARTS.split(" ".join(request.split()))
            if not _OPENS.search(part)
            for number in _ON_WORKSPACE.findall(part)
        }
    return named.pop() if len(named) == 1 and 1 <= min(named) <= 99 else None


class WindowTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str | None = Field(default=None, max_length=80, description="the window's app, as the user named it")
    title_contains: str | None = Field(
        default=None,
        max_length=120,
        description="words from the window title, only if the user mentioned them",
    )
    focused: bool = Field(default=False, description="true for 'this window' / the currently focused window")

    @model_validator(mode="after")
    def _needs_something(self) -> WindowTarget:
        if not (self.app or self.title_contains or self.focused):
            raise ValueError("say which window: app, title_contains or focused")
        return self

    def describe(self) -> str:
        if self.focused and not self.app:
            return "the focused window"
        name = f"{self.app} window" if self.app else "window"
        return name + (f" titled like '{self.title_contains}'" if self.title_contains else "")


@dataclass(frozen=True, slots=True)
class TargetMatch:
    windows: tuple[Window, ...]
    problem: str | None = None  # set when nothing matched or the app name is unknown


def window_label(window: Window, *, focused: bool = False) -> str:
    where = f"workspace {window.workspace.name}" + (", focused" if focused else "")
    return f"{window.window_class} ({where})"


def _title_has(window: Window, words: str, snapshot: WorldSnapshot) -> bool:
    """Title matching, ignoring the terminal SONU runs in: its title is the request itself."""
    return window.address != snapshot.origin_address and words.casefold() in window.title.casefold()


# "Move it to 3", "close that": the window the conversation was just about. "This" is the focused one.
_REFERS_BACK = re.compile(r"\b(it|that|them|those|the same)\b", re.IGNORECASE)
_THIS = re.compile(r"\bthis\b", re.IGNORECASE)


def refers_back(request: str) -> bool:
    return bool(_REFERS_BACK.search(request)) and not _THIS.search(request)


def find_windows(target: WindowTarget, snapshot: WorldSnapshot, ctx: ToolContext) -> TargetMatch:
    candidates: list[Window] = list(snapshot.windows)
    # Found on real hardware: "open kitty on 2" then "move it to 3" was planned as the *focused*
    # window — another terminal. The windows the last request acted on come first.
    recent = [w for w in snapshot.windows if w.address in ctx.recent_windows]
    use_recent = bool(recent) and refers_back(ctx.request_text) and not target.title_contains

    if target.focused and use_recent and not target.app:
        candidates = recent
    elif target.focused:
        active = snapshot.active_window
        if active is None:
            return TargetMatch((), "no window is focused right now")
        candidates = [active]

    if target.app:
        resolution = ctx.resolver.resolve_for_request(target.app, ctx.request_text)
        if resolution.entry:
            entries = [resolution.entry]
        elif resolution.status == "ambiguous":
            entries = list(resolution.candidates)
        else:
            entries = []
        by_entry = [
            window
            for window in candidates
            if any(
                window_matches(entry, (window.window_class, window.initial_class), ctx.learned(entry))
                for entry in entries
            )
        ]
        if not by_entry:
            # The app may not have a desktop entry: fall back to the window class itself.
            wanted = normalize(target.app)
            by_entry = [
                window
                for window in candidates
                if wanted and wanted in normalize(f"{window.window_class} {window.initial_class}")
            ]
        if not by_entry and resolution.status == "not_found":
            # Not an app at all ("the GitHub window"): treat the words as part of a title.
            by_entry = [w for w in candidates if _title_has(w, target.app, snapshot)]
        candidates = by_entry

    if target.title_contains:
        candidates = [w for w in candidates if _title_has(w, target.title_contains, snapshot)]

    if not candidates:
        return TargetMatch((), f"no {target.describe()} is open")
    if use_recent and (preferred := [w for w in candidates if w.address in ctx.recent_windows]):
        candidates = preferred
    return TargetMatch(tuple(snapshot.by_recency(candidates)))


# Words that say nothing about *which* window the user means.
_NOT_DISTINCTIVE = {
    "window",
    "windows",
    "workspace",
    "move",
    "close",
    "focus",
    "switch",
    "open",
    "show",
    "take",
    "put",
    "send",
    "bring",
    "please",
    "this",
    "that",
    "these",
    "those",
    "with",
    "from",
    "into",
    "onto",
    "over",
    "there",
    "here",
    "them",
    "then",
    "and",
    "the",
    "all",
    "every",
    "both",
    "one",
}
_ALL = re.compile(r"\b(all|every|both)\b", re.IGNORECASE)
# "all" that isn't about windows. Found on real hardware: "pin kitty on all workspaces" would
# have pinned every kitty window instead of asking which one.
_ALL_PLACES = re.compile(
    r"\b(?:all|every)(?: (?:the|my))? (?:workspaces?|desktops?|screens?|monitors?)\b", re.IGNORECASE
)


def wants_all(request: str) -> bool:
    """The user's own words ask for every matching window ("close all the kitty windows")."""
    return bool(_ALL.search(_ALL_PLACES.sub(" ", request)))


def narrow_by_request(
    windows: tuple[Window, ...], request: str, target: WindowTarget, *, ignore: str | None = None
) -> tuple[Window, ...]:
    """Pick the one window whose title contains a distinctive word from the user's request.

    Only ever *narrows* the windows the step already targeted, using the user's own (trusted)
    words — e.g. "move the GitHub window" among two browser windows. If the words don't single
    out exactly one window, nothing changes and SONU asks instead. The title of ``ignore`` (the
    terminal SONU runs in, whose title is the request itself) never counts.
    """
    if len(windows) < 2 or not request:
        return windows
    app_words = set(normalize(target.app or "").split())
    words = {
        word
        for word in re.findall(r"[a-z0-9]+", request.casefold())
        if len(word) >= 4 and not word.isdigit() and word not in _NOT_DISTINCTIVE and word not in app_words
    }
    chosen: set[str] = set()
    for word in words:
        hits = [window for window in windows if window.address != ignore and word in window.title.casefold()]
        if len(hits) == 1:
            chosen.add(hits[0].address)
    if len(chosen) == 1:
        return tuple(window for window in windows if window.address in chosen)
    return windows


def ambiguity_question(target: WindowTarget, windows: tuple[Window, ...], verb: str) -> str:
    options = "; ".join(
        f"{window.window_class} on workspace {window.workspace.name}"
        + (f" — '{window.title[:50]}'" if window.title else "")
        for window in windows[:5]
    )
    return (
        f"{len(windows)} windows match {target.describe()}: {options}. "
        f"Which one should I {verb}, or all of them?"
    )
