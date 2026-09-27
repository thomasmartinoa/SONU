"""The daemon's wire protocol: JSON lines over a private Unix socket. Standard library only —
the client imports this, and it must start in a few milliseconds.

Client → daemon:
    {"type": "request", "text", "dry_run", "json", "notify", "confirm": "terminal"|"gui"|"never", "env": {…}}
    {"type": "answer", "id", "value"}          (to a confirm or choose question)
Daemon → client:
    {"type": "plan", "kind", "model", "duration_s", "steps"}
    {"type": "step_start", "description"}
    {"type": "step", "line", "status"}
    {"type": "confirm", "id", "description", "risk", "reason"}
    {"type": "choose", "id", "title", "options": [[label, value], …]}
    {"type": "final", "status", "lines", "not_done", "questions", "headline", "report"}
    {"type": "error", "message"}
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_LINE = 1 << 20
# The client's environment the daemon may use for this request: which Hyprland session and display.
ENV_KEYS = ("HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY")


def socket_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/sonu/sonud.sock`` — a per-user directory only the user can enter."""
    env = os.environ if env is None else env
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "sonu" / "sonud.sock"


def encode(message: Mapping[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode()


def decode(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE:
        raise ValueError("message too large")
    message = json.loads(line)
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ValueError("not a SONU message")
    return message
