"""Hyprland's event stream (``.socket2.sock``).

Each event is one line: ``EVENT>>DATA\\n``. Hyprland truncates DATA to 1024 characters and
replaces newlines inside it with spaces. Fields are comma-separated, but the *last* field can
itself contain commas (window titles), so we split at most ``arity - 1`` times.

Window addresses in events have no ``0x`` prefix; we normalise them to match the JSON replies.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sonu.desktop.hyprland.errors import HyprlandRequestError
from sonu.desktop.hyprland.instance import HyprlandInstance
from sonu.desktop.hyprland.models import normalize_address

# Number of comma-separated fields per event (Hyprland wiki "IPC" + 0.56.2 source).
EVENT_ARITY: dict[str, int] = {
    "workspace": 1,
    "workspacev2": 2,
    "focusedmon": 2,
    "focusedmonv2": 2,
    "activewindow": 2,
    "activewindowv2": 1,
    "fullscreen": 1,
    "monitorremoved": 1,
    "monitorremovedv2": 3,
    "monitoradded": 1,
    "monitoraddedv2": 3,
    "createworkspace": 1,
    "createworkspacev2": 2,
    "destroyworkspace": 1,
    "destroyworkspacev2": 2,
    "moveworkspace": 2,
    "moveworkspacev2": 3,
    "renameworkspace": 2,
    "activespecial": 2,
    "activespecialv2": 3,
    "activelayout": 2,
    "openwindow": 4,
    "closewindow": 1,
    "kill": 1,
    "movewindow": 2,
    "movewindowv2": 3,
    "openlayer": 1,
    "closelayer": 1,
    "submap": 1,
    "changefloatingmode": 2,
    "urgent": 1,
    "screencast": 2,
    "screencastv2": 3,
    "windowtitle": 1,
    "windowtitlev2": 2,
    "togglegroup": 2,
    "moveintogroup": 1,
    "moveoutofgroup": 1,
    "ignoregrouplock": 1,
    "lockgroups": 1,
    "configreloaded": 0,
    "pin": 2,
    "minimized": 2,
    "bell": 1,
}

# Events whose first field is a window address.
_ADDRESS_FIRST = frozenset(
    {
        "activewindowv2",
        "openwindow",
        "closewindow",
        "kill",
        "movewindow",
        "movewindowv2",
        "changefloatingmode",
        "urgent",
        "windowtitle",
        "windowtitlev2",
        "moveintogroup",
        "moveoutofgroup",
        "pin",
        "minimized",
        "bell",
    }
)

MAX_EVENT_LINE_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class HyprlandEvent:
    name: str
    raw: str
    fields: tuple[str, ...]

    @property
    def window_address(self) -> str | None:
        """Normalised window address for window events (None when empty, e.g. focus lost)."""
        if self.name not in _ADDRESS_FIRST or not self.fields or not self.fields[0]:
            return None
        try:
            return normalize_address(self.fields[0])
        except ValueError:
            return None


def parse_event(line: str) -> HyprlandEvent | None:
    """Parse one socket2 line; returns None for lines that are not events."""
    line = line.rstrip("\n")
    name, separator, data = line.partition(">>")
    if not separator or not name:
        return None
    arity = EVENT_ARITY.get(name)
    if arity is None:  # unknown/new event: keep the raw data as a single field
        fields: tuple[str, ...] = (data,) if data else ()
    elif arity == 0:
        fields = ()
    else:
        fields = tuple(data.split(",", arity - 1))
    return HyprlandEvent(name=name, raw=data, fields=fields)


async def stream_events(
    instance: HyprlandInstance, *, connect_timeout_s: float = 2.0
) -> AsyncIterator[HyprlandEvent]:
    """Yield events until the connection closes. Reading socket2 never blocks the compositor."""
    try:
        async with asyncio.timeout(connect_timeout_s):
            reader, writer = await asyncio.open_unix_connection(
                str(instance.event_socket), limit=MAX_EVENT_LINE_BYTES
            )
    except (TimeoutError, OSError) as exc:
        raise HyprlandRequestError(
            f"cannot open Hyprland event socket {instance.event_socket}: {exc}"
        ) from exc
    try:
        skipping = False
        while True:
            try:
                raw = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError as exc:
                # Should not happen (Hyprland caps data at 1024 chars): drop the oversized line.
                await reader.readexactly(exc.consumed)
                skipping = True
                continue
            if skipping:  # tail of an oversized line
                skipping = False
                continue
            event = parse_event(raw.decode("utf-8", errors="replace"))
            if event is not None:
                yield event
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
