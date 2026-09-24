"""Finding the window(s) a step refers to — deterministically, against the current snapshot."""

from __future__ import annotations

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


def window_label(window: Window) -> str:
    return f"{window.window_class} (workspace {window.workspace.name})"


def find_windows(target: WindowTarget, snapshot: WorldSnapshot, ctx: ToolContext) -> TargetMatch:
    candidates: list[Window] = list(snapshot.windows)

    if target.focused:
        active = snapshot.active_window
        if active is None:
            return TargetMatch((), "no window is focused right now")
        candidates = [active]

    if target.app:
        resolution = ctx.resolver.resolve(target.app)
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
        candidates = by_entry

    if target.title_contains:
        needle = target.title_contains.casefold()
        candidates = [window for window in candidates if needle in window.title.casefold()]

    if not candidates:
        return TargetMatch((), f"no open window matches {target.describe()}")
    return TargetMatch(tuple(snapshot.by_recency(candidates)))


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
