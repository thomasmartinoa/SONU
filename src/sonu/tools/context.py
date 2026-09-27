from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sonu.config.settings import Settings
from sonu.core.memory import Memory
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.obs import ObsLink
from sonu.system.control import SystemControl
from sonu.world.origin import ancestor_pids

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
    # The user's own words: this request and the recent ones before it ("play it again"). What a
    # search or a network/device name may use (see grounding.py). Set by the engine.
    user_words: str = ""
    # The windows the previous request acted on (addresses): what "it" / "that" means. Set by the engine.
    recent_windows: tuple[str, ...] = ()
    # What the user asked SONU to remember (in memory only unless the runtime gives it a file).
    memory: Memory = field(default_factory=Memory)
    # SONU's parent processes, to find the terminal it was started from (tests pass `frozenset`).
    ancestors: Callable[[], frozenset[int]] = ancestor_pids
    # The browser an earlier step of this task opened ("open chrome … and search …"): web steps
    # that don't name a browser use it. Reset by the engine for every task.
    task_browser: AppEntry | None = None
    # Volume, brightness, radios, media, power mode (tests and evals pass a SystemControl on a fake).
    system: SystemControl = field(default_factory=SystemControl)
    # The clock (reminders, screenshot names); tests pass a fixed one.
    now: Callable[[], datetime] = datetime.now
    # OBS Studio's WebSocket server (tests and evals point it at a fake OBS).
    obs: ObsLink = field(default_factory=ObsLink)

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
