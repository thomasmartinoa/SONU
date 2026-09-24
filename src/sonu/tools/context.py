from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sonu.config.settings import Settings
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop

# How common terminals run a program: prefix to put before the program's argv.
TERMINAL_EXEC: dict[str, tuple[str, ...]] = {
    "kitty": ("kitty", "--"),
    "alacritty": ("alacritty", "-e"),
    "foot": ("foot",),
    "wezterm": ("wezterm", "start", "--"),
    "ghostty": ("ghostty", "-e"),
    "konsole": ("konsole", "-e"),
    "gnome-terminal": ("gnome-terminal", "--"),
}


@dataclass
class ToolContext:
    desktop: HyprlandDesktop
    resolver: AppResolver
    settings: Settings
    default_terminal: str | None = None
    which: Callable[[str], str | None] = shutil.which
    # Window classes observed for an app (desktop id -> classes), used when names don't match.
    learned_classes: dict[str, set[str]] = field(default_factory=dict)
    # The user's request for the task being run (trusted text), set by the engine.
    request_text: str = ""

    def learned(self, entry: AppEntry) -> set[str]:
        return self.learned_classes.get(entry.desktop_id, set())

    def launch_argv(self, entry: AppEntry) -> tuple[str, ...] | None:
        """The argv to start an app; terminal apps are wrapped in the user's terminal."""
        if not entry.terminal:
            return entry.argv
        candidates = [self.default_terminal] if self.default_terminal else []
        candidates += [name for name in TERMINAL_EXEC if name not in candidates]
        for name in candidates:
            if name and name in TERMINAL_EXEC and self.which(name):
                return (*TERMINAL_EXEC[name], *entry.argv)
        return None

    @staticmethod
    def executable_name(argv: tuple[str, ...]) -> str:
        return Path(argv[0]).name if argv else ""
