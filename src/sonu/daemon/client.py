"""The thin client: `sonu <request>` hands the request to the running daemon.

Standard library only, so it starts in a few hundredths of a second (the full CLI takes ~0.5 s to
import). If the daemon isn't running, or the request needs something only the full CLI does
(``--deep``, ``--model``), :func:`try_run` returns None and the CLI runs the request itself.
"""

from __future__ import annotations

import json
import os
import socket
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


def try_run(argv: list[str], out: TextIO = sys.stdout) -> int | None:
    """Run the request through the daemon; the exit code, or None if the full CLI should do it."""
    options = parse(argv)
    if options is None:
        return None
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
    with conn, conn.makefile("rb") as reader:
        conn.sendall(encode(request))
        for raw in reader:
            message = decode(raw)
            kind = message["type"]
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
