"""SONU's Control Center window (D90): ``sonu control`` starts it (its own Quickshell instance)."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

TITLE = "SONU Control Center"  # SONU's screen tools never act on a window with this title


class ControlError(Exception):
    pass


def shell_path() -> Path:
    return Path(str(resources.files("sonu.control") / "shell.qml"))


def sonu_program() -> str:
    """The ``sonu`` that's running now — the window runs the same copy (a branch's or the everyday one)."""
    started = Path(sys.argv[0])
    return (
        str(started.resolve())
        if started.name == "sonu" and started.exists()
        else shutil.which("sonu") or "sonu"
    )


def _alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text().strip())
        return str(shell_path()).encode() in Path(f"/proc/{pid}/cmdline").read_bytes()  # still our window
    except (OSError, ValueError):
        return False


def open_control(runtime_dir: Path, env: Mapping[str, str] | None = None) -> bool:
    """Start the window (``qs -n -p <shell.qml>``); False when it's already open."""
    env = dict(os.environ if env is None else env)
    program = shutil.which("qs", path=env.get("PATH"))
    if program is None:
        raise ControlError("Quickshell (qs) isn't installed — sudo pacman -S quickshell")
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid_file = runtime_dir / "control.pid"
    if _alive(pid_file):
        return False
    quiet = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in (0, 1, 2)]
    pid = os.posix_spawn(  # argv, no shell; its own process group: the window outlives the command
        program,
        [program, "-n", "-p", str(shell_path())],
        {**env, "SONU_BIN": sonu_program()},
        file_actions=quiet,
        setpgroup=0,
    )
    pid_file.write_text(f"{pid}\n")
    return True
