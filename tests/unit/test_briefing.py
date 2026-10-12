"""The briefing (D83): the day so far from SONU's own read-only tools, and once a day by itself."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from sonu.config.settings import Settings
from sonu.core.notifications import NotificationLog
from sonu.daemon.server import briefing_due
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import StepStatus
from sonu.tools.briefing_tools import brief
from sonu.tools.context import ToolContext


@pytest.fixture
async def ctx(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[ToolContext]:
    battery = tmp_path / "power_supply" / "BAT1"
    battery.mkdir(parents=True)
    (battery / "capacity").write_text("76\n")
    (battery / "status").write_text("Discharging\n")
    system = FakeSystem(crashed=[(time.time() - 600, "/usr/bin/quickshell", 11)])
    notifications = NotificationLog()
    notifications.add("Telegram", "Alice", "see you at 6")
    yield ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(),
        which=lambda name: None,
        system=SystemControl(system, power_supply=tmp_path / "power_supply", home=tmp_path),
        notifications=notifications,
        now=lambda: datetime(2026, 10, 12, 8, 5),
    )


@pytest.mark.anyio
async def test_the_briefing_says_what_its_tools_found(ctx: ToolContext) -> None:
    outcome = await brief(ctx)
    assert outcome.status is StepStatus.DONE and outcome.private
    said = outcome.summary
    assert said.startswith("Good morning — Monday 12 October, 08:05.")
    assert "Battery: 76%" in said
    assert "1 notification kept (Telegram)." in said
    assert "Crashed today: quickshell at " in said  # something went wrong: it's said


@pytest.mark.anyio
async def test_nothing_wrong_isnt_said(ctx: ToolContext) -> None:
    ctx.system.runner.crashed = []  # type: ignore[attr-defined]
    said = (await brief(ctx)).summary
    assert "crash" not in said and "failed" not in said.casefold()


def test_once_a_day_after_its_time() -> None:
    assert not briefing_due(datetime(2026, 10, 12, 7, 59), "08:00", None)
    assert briefing_due(datetime(2026, 10, 12, 8, 0), "08:00", None)
    assert briefing_due(datetime(2026, 10, 12, 14, 30), "08:00", "2026-10-11")
    assert not briefing_due(datetime(2026, 10, 12, 14, 30), "08:00", "2026-10-12")  # shown today


@pytest.mark.anyio
async def test_yesterday_is_said_once(ctx: ToolContext, tmp_path: Path) -> None:
    """Found on the laptop: "Yesterday: Yesterday (10 hours …)"."""
    from sonu.core.journal import Journal

    journal = Journal(tmp_path / "journal.sqlite3", pause_file=tmp_path / "journal-paused")
    yesterday = datetime(2026, 10, 11, 10, 0).timestamp()
    journal.record("zen", "News", yesterday)
    journal.record("kitty", "zsh", yesterday + 3600)
    ctx.journal = journal
    said = (await brief(ctx)).summary
    assert "Yesterday (" in said and "Yesterday: Yesterday" not in said
    journal.close()
