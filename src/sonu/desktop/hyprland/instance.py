"""Locating the running Hyprland instance and its IPC sockets.

Hyprland creates ``$XDG_RUNTIME_DIR/hypr/<instance signature>/`` containing:

* ``.socket.sock``  — request/response ("hyprctl") socket
* ``.socket2.sock`` — event stream socket

Normally ``HYPRLAND_INSTANCE_SIGNATURE`` tells us which instance is ours. When SONU runs
somewhere that environment was not imported (e.g. a systemd user service), we fall back to
the most recently started instance and say so.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sonu.desktop.hyprland.errors import HyprlandNotFound

REQUEST_SOCKET_NAME = ".socket.sock"
EVENT_SOCKET_NAME = ".socket2.sock"

DiscoveryMethod = Literal["environment", "newest-instance"]


@dataclass(frozen=True, slots=True)
class HyprlandInstance:
    signature: str
    directory: Path
    discovered_by: DiscoveryMethod

    @property
    def request_socket(self) -> Path:
        return self.directory / REQUEST_SOCKET_NAME

    @property
    def event_socket(self) -> Path:
        return self.directory / EVENT_SOCKET_NAME


def _runtime_dir(env: Mapping[str, str]) -> Path:
    value = env.get("XDG_RUNTIME_DIR", "")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path(f"/run/user/{os.getuid()}")


def discover_instance(env: Mapping[str, str] | None = None) -> HyprlandInstance:
    env = os.environ if env is None else env
    hypr_dir = _runtime_dir(env) / "hypr"

    signature = env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if signature:
        if "/" in signature or signature in {".", ".."}:
            raise HyprlandNotFound(f"refusing suspicious HYPRLAND_INSTANCE_SIGNATURE {signature!r}")
        directory = hypr_dir / signature
        if (directory / REQUEST_SOCKET_NAME).exists():
            return HyprlandInstance(signature, directory, "environment")
        raise HyprlandNotFound(
            f"HYPRLAND_INSTANCE_SIGNATURE is set but {directory / REQUEST_SOCKET_NAME} does not exist "
            "(stale environment from a previous session?)"
        )

    candidates = (
        sorted(
            (path for path in hypr_dir.iterdir() if (path / REQUEST_SOCKET_NAME).exists()),
            key=lambda path: (path / REQUEST_SOCKET_NAME).stat().st_mtime,
            reverse=True,
        )
        if hypr_dir.is_dir()
        else []
    )
    if not candidates:
        raise HyprlandNotFound(
            "Hyprland does not seem to be running: HYPRLAND_INSTANCE_SIGNATURE is not set "
            f"and no instance sockets were found under {hypr_dir}"
        )
    newest = candidates[0]
    return HyprlandInstance(newest.name, newest, "newest-instance")
