"""Pressing an app's buttons by name through accessibility (AT-SPI) — M6 part 2, D56.

SONU's own environment has no AT-SPI bindings; the system Python does, so ``a11y_helper.py`` runs
with it (``/usr/bin/python3``) and answers in JSON. Measured on the laptop: Thunar exposed 60 named
controls in 0.05 s; pressing "Home" moved it from /tmp to the home folder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonu.system.commands import CommandError, CommandRunner, ProgramMissing

SYSTEM_PYTHON = "/usr/bin/python3"
HELPER = Path(__file__).with_name("a11y_helper.py")
PREFERRED_ACTIONS = ("click", "press", "activate", "toggle", "jump", "open")


class AccessibilityError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Control:
    path: str
    name: str
    role: str
    actions: tuple[str, ...]
    showing: bool = False
    enabled: bool = False
    checked: bool = False
    pressed: bool = False

    @property
    def action(self) -> str | None:
        return next((a for a in PREFERRED_ACTIONS if a in self.actions), None)

    @property
    def label(self) -> str:
        return f"“{self.name}” ({self.role})"


def _control(item: dict[str, Any]) -> Control:
    return Control(
        str(item["path"]),
        str(item["name"]),
        str(item.get("role", "")),
        tuple(str(a) for a in item.get("actions", [])),
        bool(item.get("showing")),
        bool(item.get("enabled")),
        bool(item.get("checked")),
        bool(item.get("pressed")),
    )


_ERRORS = {
    "not-exposed": "doesn't show its buttons to SONU — it may not support accessibility (kitty doesn't), "
    "or it was started before accessibility was turned on (restart it)",
    "no-atspi": "can't be reached: the system's AT-SPI bindings (python-gobject, at-spi2-core) are missing",
    "moved": "changed before SONU could press it — ask again",
    "no-action": "can't be pressed",
}


class Accessibility:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def _helper(self, *args: str) -> dict[str, Any]:
        try:
            result = await self.runner.run((SYSTEM_PYTHON, str(HELPER), *args), timeout_s=15.0)
        except ProgramMissing as exc:
            raise AccessibilityError("the system Python isn't there") from exc
        except CommandError as exc:
            raise AccessibilityError(str(exc)) from exc
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise AccessibilityError("the accessibility helper gave no answer") from exc
        if not isinstance(data, dict):
            raise AccessibilityError("the accessibility helper gave no answer")
        if error := data.get("error"):
            raise AccessibilityError(_ERRORS.get(str(error), str(error)))
        return data

    async def controls(self, pid: int) -> list[Control]:
        data = await self._helper("controls", str(pid))
        return [_control(item) for item in data.get("controls", [])]

    async def press(self, pid: int, control: Control) -> tuple[bool, dict[str, Any], dict[str, Any]]:
        action = control.action or control.actions[0]
        data = await self._helper("press", str(pid), control.path, control.name, action)
        return bool(data.get("pressed")), dict(data.get("before") or {}), dict(data.get("after") or {})
