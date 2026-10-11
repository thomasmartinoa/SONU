from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sonu.config.settings import Settings
from sonu.core.journal import Journal
from sonu.core.layouts import Layouts
from sonu.core.memory import Memory
from sonu.core.notifications import NotificationLog
from sonu.core.prompt_writer import PromptWriter
from sonu.core.screen_reader import ScreenReader
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.kitty import SonuTerminals, runtime_folder
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


class WakeSwitch(Protocol):
    """ "Hey Sonu" listening, owned by sonud (the tool turns it on/off; None: no daemon)."""

    @property
    def listening(self) -> bool: ...

    async def set(self, on: bool) -> str | None:
        """Turn it on or off; a reason when it couldn't be."""
        ...


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
    # Saved window layouts ("save this layout as work"; in memory only unless the runtime gives it a file).
    layouts: Layouts = field(default_factory=Layouts)
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
    # "Hey Sonu" listening — only sonud has one (the CLI alone can't keep the microphone open).
    wake: WakeSwitch | None = None
    # SONU-owned terminals (kitty with remote control over a private socket); None: the default place.
    terminals: SonuTerminals | None = None
    # The folder an earlier step of this task made ("make a folder … and open Claude Code there").
    task_folder: Path | None = None
    # The windows earlier steps of this task opened or acted on ("open a terminal on 3 and type hello":
    # the keys go to that terminal). Reset by the engine for every task.
    task_windows: tuple[str, ...] = ()
    # Writes a prompt for Claude Code from the user's idea (the local model); None: not available.
    writer: PromptWriter | None = None
    # Reads the screen with the local vision model (M6); None: not available.
    reader: ScreenReader | None = None
    # A spoken request with "password by voice" on: says the prompt, returns what was said (never
    # shown, logged or kept). None: not available — the user types it.
    ask_secret: Callable[[str], Awaitable[str | None]] | None = None
    # The activity journal (which window had the focus, when); sonud records it. None: not available.
    journal: Journal | None = None
    # The latest notifications (D69), kept by sonud when the user turned it on. None: not kept.
    notifications: NotificationLog | None = None

    def sonu_terminals(self) -> SonuTerminals:
        if self.terminals is None:
            self.terminals = SonuTerminals(runtime_folder(), self.system.runner)
        return self.terminals

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
