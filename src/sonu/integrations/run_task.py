"""Run a project's command in SONU's window and record how it ended (D65).

Standalone (no SONU imports): SONU starts it in a SONU-owned kitty window as
``python3 run_task.py <status file> [--notify=<label>] <command…>``. The command runs with the window as its
terminal (no shell); when it ends, its exit code goes into the status file (in SONU's 0700 runtime folder),
which is what SONU reads — never the command's output, which could print anything.

``--notify``: a command that outlives SONU's wait (it said "still running") ends with a desktop notification
— "✔ tests passed in SONU (3 min)" — so the user hears back without asking (D83). Shorter runs SONU reports
itself, so they don't notify twice.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

FINISHED = "[sonu] finished"
NOTIFY_AFTER_S = 25.0  # SONU waits 30 s for a result itself: only a longer run notifies


def _took(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"


def notify(label: str, code: int, seconds: float) -> None:
    """The desktop notification a long run ends with (argv only; a missing notify-send is ignored)."""
    if seconds < NOTIFY_AFTER_S:
        return
    title = f"✔ {label} passed" if code == 0 else f"✖ {label} failed (exit code {code})"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [
                "notify-send",
                "--app-name=SONU",
                "-u",
                "normal" if code == 0 else "critical",
                "--",
                title,
                f"after {_took(seconds)} — the output is in its window",
            ],
            check=False,
            timeout=5,
        )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    status, command = Path(argv[0]), argv[1:]
    label = None
    if command and command[0].startswith("--notify="):
        label, command = command[0].removeprefix("--notify="), command[1:]
    if not command:
        return 2
    started = time.monotonic()
    print(f"$ {' '.join(command)}\n", flush=True)
    try:
        process = subprocess.Popen(command)
    except OSError as exc:
        print(f"couldn't start {command[0]}: {exc.strerror or exc}", flush=True)
        code = 127
    else:
        try:
            code = process.wait()
        except KeyboardInterrupt:  # Ctrl+C in the window: stop it
            process.terminate()
            code = process.wait()
    partial = status.with_suffix(".part")
    partial.write_text(str(code))
    partial.replace(status)
    print(f"\n{FINISHED} (exit {code})", flush=True)
    if label:
        notify(label, code, time.monotonic() - started)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
