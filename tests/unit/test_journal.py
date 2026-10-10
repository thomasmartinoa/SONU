from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.journal import MAX_SPAN_S, FocusTracker, Journal
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland import parse_event
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.base import Risk, StepStatus
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

NOW = datetime(2026, 10, 11, 15, 0)


def at(hour: int, minute: int = 0, days_ago: int = 0) -> float:
    return (NOW.replace(hour=hour, minute=minute) - timedelta(days=days_ago)).timestamp()


def minutes(spans: list[Any]) -> list[tuple[str, float]]:
    return [(span.app, round(span.seconds / 60)) for span in spans]


# ---------------------------------------------------------------- the store
def test_each_window_keeps_the_focus_until_the_next_but_at_most_half_an_hour(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "data" / "journal.sqlite3")
    journal.record("zen", "Pull Request #41 — Zen Browser", at(9))
    journal.record("code-oss", "engine.py - SONU - Code - OSS", at(9, 20))
    journal.record("kitty", "nvim dataset.py", at(10, 30))  # 70 min on Code: the user walked away
    journal.record("", "", at(10, 40))
    assert minutes(journal.spans(at(0), at(15), at(15))) == [("zen", 20), ("code-oss", 30), ("kitty", 10)]
    assert MAX_SPAN_S == 30 * 60
    # Clipped to the period asked for.
    assert minutes(journal.spans(at(9, 10), at(9, 30), at(15))) == [("zen", 10), ("code-oss", 10)]
    assert oct((tmp_path / "data" / "journal.sqlite3").stat().st_mode)[-3:] == "600"


def test_password_managers_and_private_windows_are_never_written(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path, exclude_apps=["signal"])
    journal.record("zen", "News — Zen Browser", at(9))
    journal.record("org.keepassxc.KeePassXC", "Passwords.kdbx - KeePassXC", at(9, 5))
    journal.record("zen", "My Bank — Zen Browser Private Browsing", at(9, 10))
    journal.record("chromium", "Secret stuff - Chromium (Incognito)", at(9, 15))
    journal.record("signal", "Signal — chat with Anna", at(9, 20))
    journal.record("kitty", "zsh", at(9, 25))
    journal.close()
    # The time in them only ends the window before.
    assert minutes(Journal(path).spans(at(0), at(9, 30), at(15))) == [("zen", 5), ("kitty", 5)]
    stored = path.read_bytes()
    for word in (b"Passwords", b"Bank", b"Secret", b"Anna", b"keepass", b"signal"):
        assert word not in stored


def test_a_title_changing_quickly_replaces_the_one_before(tmp_path: Path) -> None:
    journal = Journal(None)
    journal.record("zen", "Loading…", at(9))
    journal.record("zen", "(1) YouTube", at(9) + 0.5)
    journal.record("zen", "(2) YouTube", at(9) + 1.0)
    journal.record("zen", "(2) YouTube", at(9, 1))  # the same again: nothing new
    assert journal.count() == 1
    assert [span.title for span in journal.spans(at(0), at(10), at(10))] == ["(2) YouTube"]


def test_pausing_stops_recording_until_resumed(tmp_path: Path) -> None:
    pause_file = tmp_path / "journal-paused"
    journal = Journal(None, pause_file=pause_file)
    journal.record("zen", "News", at(9))
    journal.pause(True, at(9, 10))
    assert pause_file.exists() and Journal(None, pause_file=pause_file).paused  # sonud sees it too
    journal.record("kitty", "zsh", at(9, 20))
    journal.pause(False)
    journal.record("code-oss", "engine.py", at(9, 40))
    journal.record("", "", at(9, 50))
    assert minutes(journal.spans(at(0), at(10), at(10))) == [("zen", 10), ("code-oss", 10)]


def test_old_entries_go_and_forget_deletes(tmp_path: Path) -> None:
    journal = Journal(None, keep_days=30)
    journal.record("zen", "old", at(9, days_ago=40))
    journal.record("kitty", "zsh", at(9, days_ago=1))
    journal.record("code-oss", "engine.py", at(9))
    journal.prune(at(10))
    assert journal.count() == 2
    assert journal.forget(at(0)) == 1
    assert [span.app for span in journal.spans(0, at(15), at(15))] == ["kitty"]


def test_focus_events_become_entries() -> None:
    journal = Journal(None)
    tracker = FocusTracker(journal)
    tracker.start("kitty", "zsh", "0xa1")
    for line in (
        "activewindow>>zen,Pull Request #41, SONU — Zen Browser",  # a title with commas
        "activewindowv2>>b2",
        "windowtitlev2>>a1,vim notes.txt",  # not the focused window: ignored
        "windowtitlev2>>b2,Issues — Zen Browser",
        "activewindow>>,",  # nothing focused
        "activewindowv2>>",
    ):
        event = parse_event(line)
        assert event is not None
        tracker.feed(event)
    rows = journal._connect().execute("SELECT app, title FROM focus ORDER BY rowid").fetchall()
    assert rows == [
        ("kitty", "zsh"),
        ("zen", "Issues — Zen Browser"),  # the title changed within 2 s: it replaced the first
        ("", ""),
    ]


# ---------------------------------------------------------------- the tools
@dataclass
class Rig:
    ctx: ToolContext
    fake: FakeHyprland
    asked: list[ConfirmRequest] = field(default_factory=list)

    @property
    def journal(self) -> Journal:
        assert self.ctx.journal is not None
        return self.ctx.journal

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.asked.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)

    def a_day(self) -> None:
        self.journal.record("zen", "Pull Request #41 · thomasmartinoa/SONU — Zen Browser", at(9, 20))
        self.journal.record("code-oss", "engine.py - SONU - Code - OSS", at(9, 40))
        self.journal.record("kitty", "nvim dataset.py", at(10, 10))
        self.journal.record("", "", at(10, 15))
        self.journal.record("kitty", "btop", at(20, days_ago=1))
        self.journal.record("", "", at(21, days_ago=1))


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")),
        Settings(),
        which=lambda name: f"/usr/bin/{name}",
        now=lambda: NOW,
        journal=Journal(tmp_path / "journal.sqlite3", pause_file=tmp_path / "journal-paused"),
    )
    yield Rig(ctx, fake_hypr)


@pytest.mark.anyio
async def test_what_was_i_doing_today(rig: Rig) -> None:
    rig.a_day()
    report = await rig.run("what was I doing today?", ("journal.read", {"when": "today"}))
    assert report.lines() == [
        "✔ Today (55 minutes in windows): Code - OSS 30 minutes (“engine.py - SONU”); Zen Browser 20 minutes "
        "(“Pull Request #41 · thomasmartinoa/SONU”); kitty 5 minutes (“nvim dataset.py”)"
    ]
    # Window titles are personal: kept out of the audit log and the model.
    assert report.steps[0].outcome.private
    report = await rig.run("what did I do yesterday?", ("journal.read", {"when": "yesterday"}))
    assert report.lines() == ["✔ Yesterday (30 minutes in windows): kitty 30 minutes (“btop”)"]
    report = await rig.run("what was I doing this evening?", ("journal.read", {"when": "this_evening"}))
    assert report.lines() == ["✔ Nothing in the journal this evening"]


@pytest.mark.anyio
async def test_when_was_something_last_open(rig: Rig) -> None:
    rig.a_day()
    request = "when was I last on the sonu pull request?"
    report = await rig.run(request, ("journal.read", {"about": "sonu pull request"}))
    assert report.lines() == [
        "✔ “sonu pull request”: 20 minutes in the last 30 days; last today at 09:40 in Zen Browser "
        "(“Pull Request #41 · thomasmartinoa/SONU”)"
    ]
    report = await rig.run("when did I last use btop?", ("journal.read", {"about": "btop"}))
    assert report.lines()[0].endswith("last yesterday at 20:30 in kitty (“btop”)")  # an hour, capped
    report = await rig.run("when did I last open spotify?", ("journal.read", {"about": "spotify"}))
    assert report.lines() == ["✔ Nothing about “spotify” in the journal in the last 30 days"]


@pytest.mark.anyio
async def test_an_empty_journal_says_so(rig: Rig) -> None:
    report = await rig.run("what was I doing today?", ("journal.read", {"when": "today"}))
    assert report.lines() == [
        "✔ Nothing in the journal today (it has nothing yet — sonud records the windows you use from now on)"
    ]


@pytest.mark.anyio
async def test_the_journal_off_in_the_config(rig: Rig) -> None:
    rig.ctx.journal = None
    report = await rig.run("what was I doing today?", ("journal.read", {"when": "today"}))
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert "[journal] enabled = false" in report.lines()[0]


@pytest.mark.anyio
async def test_pause_and_resume_by_voice(rig: Rig) -> None:
    rig.journal.record("zen", "News", at(14))
    report = await rig.run("pause the journal", ("journal.recording", {"on": False}))
    assert report.lines() == ["✔ The journal is paused — nothing is recorded until you resume it"]
    assert rig.asked == [] and rig.journal.paused
    rig.journal.record("kitty", "zsh", at(14, 30))  # not recorded
    rig.fake.windows.append(make_window("0xc3", "code-oss", "engine.py - SONU - Code - OSS", workspace_id=1))
    rig.fake.active_address = "0xc3"
    report = await rig.run("resume the journal", ("journal.recording", {"on": True}))
    assert report.lines() == ["✔ The journal is recording again"]
    # The window you're in counts from now (NOW = 15:00).
    assert minutes(rig.journal.spans(at(0), at(15, 10), at(15, 10))) == [("zen", 30), ("code-oss", 10)]


@pytest.mark.anyio
async def test_forgetting_asks_first(rig: Rig) -> None:
    rig.a_day()
    report = await rig.run("delete today's journal", ("journal.forget", {"since": "today"}), approve=False)
    assert rig.asked[0].risk is Risk.MEDIUM
    assert "Delete today's journal (3 windows) — it can't be undone" in rig.asked[0].description
    assert rig.journal.count() == 4 and report.status != "done"
    report = await rig.run("delete today's journal", ("journal.forget", {"since": "today"}))
    assert report.lines() == ["✔ Deleted today's journal"]
    assert rig.journal.count() == 1  # yesterday's stays


def test_a_spinner_or_a_count_in_front_of_a_title_is_the_same_title() -> None:
    # Found on the laptop: Claude Code's spinner changed its terminal's title every second, a row every 2 s.
    journal = Journal(None)
    for second, title in enumerate(
        ["◑ Fix the tests", "◐ Fix the tests", "◓ Fix the tests", "✳ Fix the tests"]
    ):
        journal.record("kitty", title, at(9) + second * 3)
    journal.record("zen", "(1) Inbox — Zen Browser", at(9, 10))
    journal.record("zen", "(2) Inbox — Zen Browser", at(9, 15))
    journal.record("", "", at(9, 20))
    assert journal.count() == 2
    assert minutes(journal.spans(at(0), at(10), at(10))) == [("kitty", 10), ("zen", 10)]
