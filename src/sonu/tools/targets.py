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
        name = self.app or "window"
        return f"{name} window" + (f" titled like '{self.title_contains}'" if self.title_contains else "")


@dataclass(frozen=True, slots=True)
class TargetMatch:
    windows: tuple[Window, ...]
    problem: str | None = None  # set when nothing matched or the app name is unknown


def window_label(window: Window, *, focused: bool = False) -> str:
    where = f"workspace {window.workspace.name}" + (", focused" if focused else "")
    return f"{window.window_class} ({where})"


def find_windows(target: WindowTarget, snapshot: WorldSnapshot, ctx: ToolContext) -> TargetMatch:
    candidates: list[Window] = list(snapshot.windows)

    if target.focused:
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
            needle = target.app.casefold()
            by_entry = [window for window in candidates if needle in window.title.casefold()]
        candidates = by_entry

    if target.title_contains:
        needle = target.title_contains.casefold()
        candidates = [window for window in candidates if needle in window.title.casefold()]

    if not candidates:
        return TargetMatch((), f"no open window matches {target.describe()}")
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


def wants_all(request: str) -> bool:
    """The user's own words ask for every matching window ("close all the kitty windows")."""
    return bool(_ALL.search(request))


def narrow_by_request(windows: tuple[Window, ...], request: str, target: WindowTarget) -> tuple[Window, ...]:
    """Pick the one window whose title contains a distinctive word from the user's request.

    Only ever *narrows* the windows the step already targeted, using the user's own (trusted)
    words — e.g. "move the GitHub window" among two browser windows. If the words don't single
    out exactly one window, nothing changes and SONU asks instead.
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
        hits = [window for window in windows if word in window.title.casefold()]
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
