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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
    "powerprofilesctl": "power-profiles-daemon",
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

    async def run(self, argv: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> CommandResult: ...


class SubprocessRunner:
    def __init__(self, which: Callable[[str], str | None] = shutil.which) -> None:
        self.which = which

    def available(self, program: str) -> bool:
        return self.which(program) is not None

    async def run(self, argv: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> CommandResult:
        path = self.which(argv[0])
        if path is None:
            raise ProgramMissing(argv[0])
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                *argv[1:],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise CommandError(f"Couldn't start {argv[0]}: {exc}") from exc
        try:
            async with asyncio.timeout(timeout_s):
                out, err = await process.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise CommandError(f"{argv[0]} didn't finish within {timeout_s:.0f}s") from None
        return CommandResult(
            tuple(argv),
            process.returncode or 0,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )
