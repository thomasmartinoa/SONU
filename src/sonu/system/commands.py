"""Running system programs safely: an argv list (never a shell), a timeout, and the C locale.

Only SONU's own code decides the argv — tools pass validated numbers and fixed words, never text
from the model or from window titles. Output is read in the C locale so "enabled" stays "enabled"
whatever language the desktop uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_TIMEOUT_S = 5.0

# Program → the Arch package that provides it (shown when a program is missing).
PACKAGES: dict[str, str] = {
    "wpctl": "wireplumber",
    "brightnessctl": "brightnessctl",
    "bluetoothctl": "bluez-utils",
    "rfkill": "util-linux",
    "nmcli": "networkmanager",
    "playerctl": "playerctl",
    "pw-dump": "pipewire",
    "powerprofilesctl": "power-profiles-daemon",
    "yt-dlp": "yt-dlp",
    "grim": "grim",
    "slurp": "slurp",
    "wl-copy": "wl-clipboard",
    "wl-paste": "wl-clipboard",
    "systemd-run": "systemd",
    "systemctl": "systemd",
    "notify-send": "libnotify",
    "xdg-mime": "xdg-utils",
    "qs": "quickshell",
    "nvidia-smi": "nvidia-utils",
}


class CommandError(Exception):
    """A system program failed or couldn't be run."""


class ProgramMissing(CommandError):
    def __init__(self, program: str) -> None:
        self.program = program
        package = PACKAGES.get(program)
        hint = f" (install: sudo pacman -S {package})" if package else ""
        super().__init__(f"{program} isn't installed{hint}")


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def problem(self) -> str:
        """A short description of why the command failed, for reports."""
        text = (self.stderr or self.stdout).strip().splitlines()
        detail = text[-1][:160] if text else f"exit code {self.returncode}"
        return f"{self.argv[0]} failed: {detail}"


class CommandRunner(Protocol):
    def available(self, program: str) -> bool: ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        input_bytes: bytes | None = None,
        capture: bool = True,
    ) -> CommandResult: ...


def program_env(
    environ: dict[str, str] | None = None, *, prefix: str = sys.prefix, base_prefix: str = sys.base_prefix
) -> dict[str, str]:
    """The environment for system programs: the C locale, and none of SONU's own virtualenv.

    Found on real hardware: under ``uv run`` the virtualenv's ``bin`` comes first on PATH, so
    ``#!/usr/bin/env python3`` programs (powerprofilesctl) got SONU's Python instead of the
    system's, without the modules they need ("No module named 'gi'").
    """
    env = dict(os.environ if environ is None else environ)
    if prefix != base_prefix:  # running inside a virtualenv
        venv_bin = (Path(prefix) / "bin").resolve()
        paths = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(p for p in paths if p and Path(p).resolve() != venv_bin)
        env.pop("VIRTUAL_ENV", None)
        env.pop("VIRTUAL_ENV_PROMPT", None)
    return {**env, "LC_ALL": "C", "LANG": "C"}


class SubprocessRunner:
    def __init__(self, which: Callable[[str], str | None] = shutil.which) -> None:
        self.which = which

    def available(self, program: str) -> bool:
        return self.which(program) is not None

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        input_bytes: bytes | None = None,
        capture: bool = True,
    ) -> CommandResult:
        """``capture=False`` for programs that fork a background helper (wl-copy): the helper would
        keep our output pipes open, so their output goes to /dev/null and only the exit is awaited."""
        path = self.which(argv[0])
        if path is None:
            raise ProgramMissing(argv[0])
        env = program_env()
        output = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                *argv[1:],
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                env=env,
            )
        except OSError as exc:
            raise CommandError(f"Couldn't start {argv[0]}: {exc}") from exc
        try:
            async with asyncio.timeout(timeout_s):
                out, err = await process.communicate(input_bytes)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise CommandError(f"{argv[0]} didn't finish within {timeout_s:.0f}s") from None
        return CommandResult(
            tuple(argv),
            process.returncode or 0,
            (out or b"").decode("utf-8", errors="replace"),
            (err or b"").decode("utf-8", errors="replace"),
        )
