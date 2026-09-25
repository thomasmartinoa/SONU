"""Which window SONU was started from.

When you type ``sonu move the youtube window to 3`` in a terminal, that terminal's title usually
becomes the command itself — so it "contains" every word of the request. Title-based matching
must never treat that echo as evidence (it once made "go to youtube" focus the terminal).

The terminal is found deterministically: it is the window whose process is an ancestor of SONU's
own process (kitty → zsh → sonu). Nothing is guessed from titles.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from sonu.desktop.hyprland.models import Window

logger = logging.getLogger(__name__)

_MAX_DEPTH = 64


def ancestor_pids(pid: int | None = None, *, proc: Path = Path("/proc")) -> frozenset[int]:
    """The parent, grandparent, … of ``pid`` (default: this process), excluding init."""
    found: set[int] = set()
    current = pid if pid is not None else os.getpid()
    for _ in range(_MAX_DEPTH):
        try:
            stat = (proc / str(current) / "stat").read_text()
        except OSError:
            break
        try:
            # "pid (comm) state ppid …" — comm may contain spaces and parentheses.
            parent = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            logger.debug("unexpected /proc/%s/stat format", current)
            break
        if parent <= 1 or parent in found:
            break
        found.add(parent)
        current = parent
    return frozenset(found)


def find_origin(
    windows: Iterable[Window], ancestors: frozenset[int], active_address: str | None
) -> str | None:
    """The address of the window SONU runs in, or None (e.g. started from a keybind).

    A terminal that serves several windows from one process (kitty --single-instance) is only
    counted when the window is the focused one, since that's where the command was typed.
    """
    matches = [window for window in windows if window.pid > 1 and window.pid in ancestors]
    if len(matches) == 1:
        return matches[0].address
    return next((window.address for window in matches if window.address == active_address), None)
