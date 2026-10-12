"""``sonu control``: open SONU's Control Center — every setting without editing the file (D90)."""

from __future__ import annotations

import typer

from sonu.cli.state import get_state
from sonu.control import ControlError, open_control
from sonu.daemon.protocol import socket_path


def control(ctx: typer.Context) -> None:
    """Open SONU's Control Center: the brain, voice, heads-ups, privacy — no config file to edit."""
    state = get_state(ctx)
    try:
        opened = open_control(socket_path().parent)
    except ControlError as exc:
        state.err.print(f"[red]✖[/] {exc}")
        raise typer.Exit(1) from None
    state.console.print(
        "SONU's Control Center is open." if opened else "SONU's Control Center is already open."
    )
