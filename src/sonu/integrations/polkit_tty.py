"""Run a command whose password prompt appears in *this* terminal (SONU's install window) instead of
the GUI dialog — so the user can type it there, or say it and SONU enters it into its own window.

Standalone (no SONU imports): SONU starts it in a SONU-owned kitty window as
``python3 polkit_tty.py <command…>``. It registers polkit's text agent (``pkttyagent``) for itself
and for the command it starts (``pkexec`` asks on behalf of its caller: this process for
``pkexec pacman``, the command itself for ``yay --sudo pkexec``), then runs the command.
"""

from __future__ import annotations

import os
import subprocess
import sys

FINISHED = "[sonu] finished"


def _agent(pid: int, *, wait: bool) -> subprocess.Popen[bytes] | None:
    read_fd, write_fd = os.pipe()
    try:
        agent = subprocess.Popen(
            ["pkttyagent", "--process", str(pid), "--notify-fd", str(write_fd)], pass_fds=(write_fd,)
        )
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        return None
    os.close(write_fd)
    if wait:
        os.read(read_fd, 1)  # closed by the agent once it's registered
    os.close(read_fd)
    return agent


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    agents = [_agent(os.getpid(), wait=True)]
    command = subprocess.Popen(argv)
    agents.append(_agent(command.pid, wait=False))
    try:
        code = command.wait()
    except KeyboardInterrupt:  # Ctrl+C in the window: stop quietly
        command.terminate()
        code = 130
    finally:
        for agent in agents:
            if agent is not None:
                agent.terminate()
    print(f"\n{FINISHED} (exit {code})", flush=True)  # SONU reads this: the command is over
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
