"""Optional integration with the ``hypr-dotfiles`` rice (Quickshell shell + ``hypr-theme``).

SONU only *reads* here. It picks up the user's chosen default apps so "open my browser" means
the browser the user actually picked, and it detects the Quickshell shell so later milestones
can offer a menu entry and a bar widget.

The rice stores defaults in ``~/.config/hypr-theme/defaults/{terminal,browser,agent}``; when a
file is missing, the rice itself falls back to kitty and zen-browser (``binds.lua`` and
``hypr-default``), and SONU mirrors that so both agree. The file manager (SUPER+E) is set in
``~/.config/hypr/modules/binds.lua`` as ``local fileManager = "thunar"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
DEFAULT_KINDS = ("browser", "terminal", "agent")
# The rice's own fallbacks (hyprland/.config/hypr/modules/binds.lua, theme/.local/bin/hypr-default).
RICE_FALLBACKS = {"terminal": "kitty", "browser": "zen-browser"}

DefaultSource = Literal["file", "rice-fallback", "binds"]
_FILE_MANAGER = re.compile(r'^\s*local\s+fileManager\s*=\s*"([^"]+)"', re.MULTILINE)


@dataclass(frozen=True, slots=True)
class DefaultApp:
    command: str
    source: DefaultSource


@dataclass(frozen=True, slots=True)
class HyprThemeInfo:
    theme_dir_present: bool = False
    defaults: dict[str, DefaultApp] = field(default_factory=dict)
    quickshell_config: bool = False
    menu_local_file: bool = False

    @property
    def detected(self) -> bool:
        return self.theme_dir_present or self.quickshell_config


def _read_default(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    return first_line if _APP_NAME.match(first_line) else None


def _binds_file_manager(home: Path) -> str | None:
    try:
        text = (home / ".config" / "hypr" / "modules" / "binds.lua").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _FILE_MANAGER.search(text)
    return match.group(1) if match and _APP_NAME.match(match.group(1)) else None


def detect_hypr_theme(home: Path) -> HyprThemeInfo:
    theme_dir = home / ".config" / "hypr-theme"
    theme_dir_present = theme_dir.is_dir()
    defaults: dict[str, DefaultApp] = {}
    for kind in DEFAULT_KINDS:
        value = _read_default(theme_dir / "defaults" / kind)
        if value:
            defaults[kind] = DefaultApp(value, "file")
        elif theme_dir_present and kind in RICE_FALLBACKS:
            defaults[kind] = DefaultApp(RICE_FALLBACKS[kind], "rice-fallback")
    if (file_manager := _binds_file_manager(home)) is not None:
        defaults["files"] = DefaultApp(file_manager, "binds")
    return HyprThemeInfo(
        theme_dir_present=theme_dir_present,
        defaults=defaults,
        quickshell_config=(home / ".config" / "quickshell" / "shell.qml").is_file(),
        menu_local_file=(theme_dir / "menu.local.jsonc").is_file(),
    )
