from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from sonu.config.paths import SonuPaths
from sonu.config.settings import Settings

Status = Literal["ok", "info", "warn", "fail", "skip"]
STATUS_ORDER: tuple[Status, ...] = ("fail", "warn", "ok", "info", "skip")


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    group: str
    title: str
    status: Status
    summary: str
    hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "hint": self.hint,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(argv: Sequence[str], timeout_s: float) -> CommandResult | None:
    """Run a program (never through a shell). None if it is missing or times out."""
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s, check=False)
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return None
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(slots=True)
class DoctorContext:
    """Everything the checks touch, injectable so tests never depend on the host machine."""

    env: Mapping[str, str]
    paths: SonuPaths
    settings: Settings
    settings_from_file: bool
    settings_error: str | None = None
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[Sequence[str], float], CommandResult | None] = run_command
    ollama_transport: httpx.AsyncBaseTransport | None = None
    platform: str = sys.platform
    python_version: tuple[int, int, int] = (sys.version_info[0], sys.version_info[1], sys.version_info[2])
    home: Path = field(default_factory=Path.home)
    getuid: Callable[[], int] = os.getuid
    # Facts gathered by earlier checks for later ones (e.g. GPU memory for model-fit checks).
    facts: dict[str, Any] = field(default_factory=dict)
