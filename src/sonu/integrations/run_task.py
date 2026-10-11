"""Run a project's command in SONU's window and record how it ended (D65).

Standalone (no SONU imports): SONU starts it in a SONU-owned kitty window as
``python3 run_task.py <status file> <command…>``. The command runs with the window as its terminal
(no shell); when it ends, its exit code goes into the status file (in SONU's 0700 runtime folder),
which is what SONU reads — never the command's output, which could print anything.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FINISHED = "[sonu] finished"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    status, command = Path(argv[0]), argv[1:]
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
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
