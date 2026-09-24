"""A consistent, read-only picture of the desktop at one moment.

Everything SONU decides and verifies is based on a fresh snapshot — never on what it *expects*
the desktop to look like after an action.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sonu.desktop.hyprland.models import Monitor, Window, Workspace


@dataclass(frozen=True, slots=True)
class WindowRef:
    """A way to find the same window again: its stable id when available, else its address."""

    address: str
    stable_id: str | None = None

    @classmethod
    def of(cls, window: Window) -> WindowRef:
        return cls(window.address, window.stable_id)

    def find(self, windows: Iterable[Window]) -> Window | None:
        for window in windows:
            if self.stable_id and window.stable_id == self.stable_id:
                return window
            if not self.stable_id and window.address == self.address:
                return window
        return None


@dataclass(frozen=True, slots=True)
class WorldSnapshot:
    windows: tuple[Window, ...]
    workspaces: tuple[Workspace, ...] = ()
    monitors: tuple[Monitor, ...] = ()
    active_address: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def addresses(self) -> frozenset[str]:
        return frozenset(window.address for window in self.windows)

    @property
    def active_window(self) -> Window | None:
        if self.active_address is None:
            return None
        return next((window for window in self.windows if window.address == self.active_address), None)

    @property
    def _focused_monitor(self) -> Monitor | None:
        focused = next((monitor for monitor in self.monitors if monitor.focused), None)
        return focused or (self.monitors[0] if self.monitors else None)

    @property
    def active_workspace_id(self) -> int | None:
        monitor = self._focused_monitor
        return monitor.active_workspace.id if monitor else None

    @property
    def active_workspace_name(self) -> str | None:
        monitor = self._focused_monitor
        return monitor.active_workspace.name if monitor else None

    def find(self, ref: WindowRef) -> Window | None:
        return ref.find(self.windows)

    def new_windows(self, before: WorldSnapshot) -> tuple[Window, ...]:
        old = before.addresses
        return tuple(window for window in self.windows if window.address not in old)

    def by_recency(self, windows: Iterable[Window]) -> list[Window]:
        """Most recently focused first (Hyprland's focusHistoryID: 0 = current)."""
        return sorted(windows, key=lambda w: w.focus_history_id if w.focus_history_id >= 0 else 1_000_000)

    def describe(self, *, include_titles: bool = True, max_title: int = 60) -> str:
        """Compact text for the planner. Titles are app/web content, so they're quoted as data."""
        lines: list[str] = []
        active_ws = self.active_workspace_id
        lines.append(f"focused workspace: {active_ws if active_ws is not None else 'unknown'}")
        active = self.active_window
        if active is not None:
            lines.append(f"focused window: {active.window_class} on workspace {active.workspace.name}")
        by_ws: dict[str, list[Window]] = {}
        for window in self.windows:
            by_ws.setdefault(window.workspace.name, []).append(window)
        if not by_ws:
            lines.append("no windows are open")

        def sort_key(name: str) -> tuple[int, int | str]:
            return (0, int(name)) if name.isdigit() else (1, name)

        for workspace in sorted(by_ws, key=sort_key):
            parts = []
            for window in by_ws[workspace]:
                if include_titles and window.title:
                    title = (
                        window.title
                        if len(window.title) <= max_title
                        else window.title[: max_title - 1] + "…"
                    )
                    parts.append(f"{window.window_class} (title: {title!r})")
                else:
                    parts.append(window.window_class)
            lines.append(f"workspace {workspace}: " + "; ".join(parts))
        return "\n".join(lines)
