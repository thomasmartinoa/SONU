"""The thin client: `sonu <request>` hands the request to the running daemon.

Standard library only, so it starts in a few hundredths of a second (the full CLI takes ~0.5 s to
import). If the daemon isn't running, or the request needs something only the full CLI does
(``--deep``, ``--model``), :func:`try_run` returns None and the CLI runs the request itself.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from typing import Any, TextIO

from sonu.daemon.protocol import ENV_KEYS, decode, encode, socket_path

CONNECT_TIMEOUT_S = 0.3
_COMMANDS = {"ask", "prompt", "eval", "doctor", "config", "audit", "desktop", "daemon", "voice"}
_FLAGS = {"--dry-run", "--json", "--notify", "-v", "-vv", "-vvv", "--verbose"}
_COLOURS = {
    "done": "32",
    "planned": "34",
    "partial": "33",
    "unverified": "33",
    "declined": "33",
    "needs-input": "36",
}
SUCCESS = {"done", "planned", "reply", "ask"}  # exit 0, as the CLI (ask_cmd.SUCCESS_STATUSES)


def parse(argv: list[str]) -> dict[str, Any] | None:
    """`sonu [--dry-run] [--json] [--notify] [--confirm X] <request…>` → options; None: not for us."""
    options: dict[str, Any] = {
        "type": "request",
        "dry_run": False,
        "json": False,
        "notify": False,
        "confirm": "auto",
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in _FLAGS:
            key = arg.lstrip("-").replace("-", "_")
            if key in options:
                options[key] = True
        elif arg == "--confirm" and index + 1 < len(argv):
            options["confirm"] = argv[index + 1]
            index += 1
        elif arg.startswith("--confirm="):
            options["confirm"] = arg.partition("=")[2]
        elif arg.startswith("-"):
            return None  # --deep, --model, --help …: the full CLI handles those
        else:
            break
        index += 1
    words = argv[index:]
    if words == ["listen"]:  # push-to-talk: the SONU key runs `sonu listen`
        options["type"] = "listen"
        words = []
    elif not words or words[0] in _COMMANDS:
        return None
    if options["confirm"] not in ("auto", "terminal", "gui", "never"):
        return None
    if options["confirm"] == "auto":
        options["confirm"] = "terminal" if sys.stdin.isatty() else "gui"
    options["text"] = " ".join(words)
    return options


class Notice:
    """One desktop notification, updated in place (``notify-send -p`` / ``--replace-id``).

    Found on the laptop: the SONU key runs `sonu listen` with no terminal, so nothing showed that
    SONU was listening, what it heard, or what it did.
    """

    def __init__(self, program: str | None = None) -> None:
        self.program = program if program is not None else shutil.which("notify-send")
        self.id = ""

    def show(self, title: str, body: str = "", *, seconds: float = 8.0) -> None:
        if not self.program:
            return
        argv = [self.program, "--app-name=SONU", "--transient", "-p", f"--expire-time={int(seconds * 1000)}"]
        if self.id:
            argv.append(f"--replace-id={self.id}")
        try:  # "--": the heard words can never be read as a flag
            done = subprocess.run(
                [*argv, "--", title, body[:300]], capture_output=True, text=True, timeout=2, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if done.stdout.strip().isdigit():
            self.id = done.stdout.strip()


class Progress:
    """A spoken request on the desktop (no terminal): one notification, updated as it goes —
    listening → what was heard → the result. Used by the key's client and by "Hey Sonu" in sonud."""

    def __init__(self, notice: Notice) -> None:
        self.notice = notice
        self.lines: list[str] = []  # the step lines so far

    def update(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "listening":
            self.notice.show("🎙 Listening…", "Say what you need", seconds=20)
        elif kind == "heard":
            self.notice.show(f"You: {message.get('text') or '(nothing)'}", "Working on it…", seconds=30)
        elif kind == "step":
            self.lines.append(str(message.get("line", "")))
        elif kind == "error":
            self.notice.show("SONU", f"✖ {message.get('message')}")
        elif kind == "final":
            lines = [*self.lines, *message.get("before_headline", []), message.get("headline") or ""]
            self.notice.show("SONU", "\n".join(line for line in lines if line))


def _style(text: str, code: str, out: TextIO) -> str:
    return f"\033[{code}m{text}\033[0m" if out.isatty() else text


def _ask_yes_no(message: dict[str, Any], out: TextIO) -> bool:
    risk = message.get("risk", "")
    out.write(
        f"{_style('?', '33', out)} {message.get('description')} ({risk} risk: {message.get('reason')})\n"
    )
    try:
        answer = input("  Allow it? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _ask_choice(message: dict[str, Any], out: TextIO) -> str | None:
    options = message.get("options", [])
    out.write(f"{_style('?', '36', out)} {message.get('title')}\n")
    for number, (label, _) in enumerate(options, 1):
        out.write(f"  {number}. {label}\n")
    try:
        answer = input("  Number (Enter to cancel): ").strip()
    except EOFError:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return str(options[int(answer) - 1][1])
    return None


def try_run(argv: list[str], out: TextIO = sys.stdout, notice: Notice | None = None) -> int | None:
    """Run the request through the daemon; the exit code, or None if the full CLI should do it."""
    options = parse(argv)
    if options is None:
        return None
    if notice is None and options["type"] == "listen" and not out.isatty():
        notice = Notice()  # started from the SONU key: show it on the desktop
    path = socket_path()
    if not path.exists():
        return None
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(CONNECT_TIMEOUT_S)
    try:
        conn.connect(str(path))
    except OSError:
        conn.close()
        return None  # a stale socket: the daemon isn't running
    conn.settimeout(None)
    as_json = options["json"]
    request = {
        "type": "request",
        **options,
        "env": {key: os.environ[key] for key in ENV_KEYS if key in os.environ},
    }
    progress = Progress(notice) if notice is not None else None
    with conn, conn.makefile("rb") as reader:
        conn.sendall(encode(request))
        for raw in reader:
            message = decode(raw)
            kind = message["type"]
            if progress is not None:
                progress.update(message)
            if kind == "plan" and message.get("kind") == "steps" and not as_json:
                steps = message["steps"]
                out.write(
                    _style(
                        f"plan by {message['model']} in {message['duration_s']:.1f}s · {steps} step"
                        f"{'s' if steps != 1 else ''}",
                        "2",
                        out,
                    )
                    + "\n"
                )
            elif kind == "listening" and not as_json:
                out.write(_style("🎙 Listening…", "36", out) + "\n")
            elif kind == "heard" and not as_json:
                out.write(_style(f"You: {message['text'] or '(nothing)'}", "2", out) + "\n")
            elif kind == "step" and not as_json:
                out.write(message["line"] + "\n")
            elif kind == "confirm":
                conn.sendall(
                    encode({"type": "answer", "id": message["id"], "value": _ask_yes_no(message, out)})
                )
            elif kind == "choose":
                conn.sendall(
                    encode({"type": "answer", "id": message["id"], "value": _ask_choice(message, out)})
                )
            elif kind == "error":
                out.write(_style(f"✖ {message['message']}", "31", out) + "\n")
                return 1
            elif kind == "final":
                if as_json:
                    out.write(json.dumps(message["report"], indent=2, ensure_ascii=False) + "\n")
                else:
                    for line in message.get("before_headline", []):
                        out.write(line + "\n")
                    headline = message.get("headline")
                    if headline:
                        out.write(_style(headline, _COLOURS.get(message["status"], "31"), out) + "\n")
                out.flush()
                return 0 if message["status"] in SUCCESS else 1
            out.flush()
    out.write(_style("✖ The SONU daemon stopped before answering", "31", out) + "\n")
    return 1
