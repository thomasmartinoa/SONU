"""The Hyprland desktop adapter: observe, act and wait — the only way SONU's tools touch Hyprland."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TypeVar

from sonu.desktop.hyprland.dialect import Dialect, WindowSelector, WorkspaceTarget, dialect_for
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.events import stream_events
from sonu.desktop.hyprland.instance import HyprlandInstance
from sonu.desktop.hyprland.ipc import HyprlandClient
from sonu.desktop.hyprland.models import Window
from sonu.world.snapshot import WorldSnapshot

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_POLL_S = 0.5


class HyprlandDesktop:
    def __init__(self, client: HyprlandClient, dialect: Dialect) -> None:
        self.client = client
        self.dialect = dialect

    @property
    def instance(self) -> HyprlandInstance:
        return self.client.instance

    @classmethod
    async def connect(cls, instance: HyprlandInstance, *, timeout_s: float = 2.0) -> HyprlandDesktop:
        client = HyprlandClient(instance, timeout_s=timeout_s)
        return cls(client, dialect_for(await client.config_provider()))

    # ------------------------------------------------------------------ observe
    async def snapshot(self) -> WorldSnapshot:
        windows, workspaces, monitors, active = await asyncio.gather(
            self.client.clients(),
            self.client.workspaces(),
            self.client.monitors(),
            self.client.active_window(),
        )
        return WorldSnapshot(
            windows=tuple(window for window in windows if window.mapped and not window.hidden),
            workspaces=tuple(workspaces),
            monitors=tuple(monitors),
            active_address=active.address if active else None,
        )

    async def wait_for(
        self,
        predicate: Callable[[WorldSnapshot], T | None],
        *,
        timeout_s: float,
        poll_s: float = DEFAULT_POLL_S,
    ) -> tuple[T | None, WorldSnapshot]:
        """Re-check ``predicate`` on fresh snapshots until it returns something truthy or time runs out.

        Hyprland events wake the loop early; polling is the fallback, so a missed event can only
        delay a result, never lose it. Returns the predicate's last value and the snapshot it saw.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        wake = asyncio.Event()

        async def listen() -> None:
            try:
                async for _event in stream_events(self.instance):
                    wake.set()
            except HyprlandError as exc:  # polling still works without events
                logger.debug("event stream unavailable while waiting: %s", exc)

        listener = asyncio.create_task(listen())
        try:
            while True:
                wake.clear()  # before the snapshot, so events during it aren't lost
                snapshot = await self.snapshot()
                result = predicate(snapshot)
                if result:
                    return result, snapshot
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return result, snapshot
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(min(poll_s, remaining)):
                        await wake.wait()
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    # ------------------------------------------------------------------ act
    def selector(self, window: Window) -> WindowSelector:
        return WindowSelector.for_window(window, prefer_stable_id=self.dialect.supports_stable_id)

    async def launch(self, argv: Sequence[str], workspace: WorkspaceTarget | None = None) -> None:
        await self.client.dispatch(self.dialect.exec(argv, workspace=workspace))

    async def move(self, window: Window, workspace: WorkspaceTarget) -> None:
        await self.client.dispatch(self.dialect.move_to_workspace(self.selector(window), workspace))

    async def focus(self, window: Window) -> None:
        await self.client.dispatch(self.dialect.focus_window(self.selector(window)))

    async def show_workspace(self, workspace: WorkspaceTarget) -> None:
        await self.client.dispatch(self.dialect.focus_workspace(workspace))

    async def close(self, window: Window) -> None:
        """Politely ask the window to close (never SIGKILL)."""
        await self.client.dispatch(self.dialect.close_window(self.selector(window)))
