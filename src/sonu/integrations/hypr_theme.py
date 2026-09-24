"""Optional integration with the ``hypr-dotfiles`` rice (Quickshell shell + ``hypr-theme``).

SONU only *reads* here. It picks up the user's chosen default apps
(``~/.config/hypr-theme/defaults/{browser,terminal,agent}``) so "open my browser" means the
browser the user actually picked, and it detects the Quickshell shell so later milestones can
offer a menu entry and a bar widget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
DEFAULT_KINDS = ("browser", "terminal", "agent")


@dataclass(frozen=True, slots=True)
class HyprThemeInfo:
    defaults: dict[str, str] = field(default_factory=dict)
    quickshell_config: bool = False
    menu_local_file: bool = False

    @property
    def detected(self) -> bool:
        return bool(self.defaults) or self.quickshell_config


def _read_default(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    return first_line if _APP_NAME.match(first_line) else None


def detect_hypr_theme(home: Path) -> HyprThemeInfo:
    theme_dir = home / ".config" / "hypr-theme"
    defaults: dict[str, str] = {}
    for kind in DEFAULT_KINDS:
        value = _read_default(theme_dir / "defaults" / kind)
        if value:
            defaults[kind] = value
    return HyprThemeInfo(
        defaults=defaults,
        quickshell_config=(home / ".config" / "quickshell" / "shell.qml").is_file(),
        menu_local_file=(theme_dir / "menu.local.jsonc").is_file(),
    )
