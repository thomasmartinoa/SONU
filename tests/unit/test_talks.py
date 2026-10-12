"""Conversations remembered (D86): what the user and SONU said, kept locally, looked back at, forgotten."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.planning import Plan
from sonu.core.report import TaskReport
from sonu.core.talks import Talks
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

NOW = datetime(2026, 10, 12, 15, 0)


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def test_kept_privately_searched_and_forgotten(tmp_path: Path) -> None:
    talks = Talks(tmp_path / "conversations.sqlite3", keep_days=30)
    talks.add("find out how kestrels hunt", "Kestrels hover and drop onto voles.", at(11, 14))
    talks.add("open kitty on 2", "Done, kitty's on 2.", at(12, 9))
    talks.add("what's 100% of it_x", "All of it.", at(12, 10))
    assert stat.S_IMODE((tmp_path / "conversations.sqlite3").stat().st_mode) == 0o600
    assert [e.request for e in talks.between(at(12, 0), at(13, 0))] == [
        "open kitty on 2",
        "what's 100% of it_x",
    ]
    assert [e.request for e in talks.between(0, at(13, 0), ("KESTRELS",))] == ["find out how kestrels hunt"]
    assert [e.request for e in talks.between(0, at(13, 0), ("voles", "hover"))] == [
        "find out how kestrels hunt"
    ]
    assert talks.between(0, at(13, 0), ("%",)) == talks.between(0, at(13, 0), ("100%",))  # % is a letter here
    assert len(talks.between(0, at(13, 0), ("_",))) == 1
    talks.prune(at(11, 14) + 31 * 86400)  # a month later
    assert talks.count() == 0
    talks.add("hi", "Hi!", at(12, 11))
    assert talks.forget(at(12, 0)) == 1 and talks.count() == 0
    talks.close()


class Rig:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.asked: list[str] = []

    async def run(
        self, request: str, *steps: tuple[str, dict[str, Any]], dry_run: bool = False
    ) -> TaskReport:
        async def confirm(asking: ConfirmRequest) -> bool:
            self.asked.append(asking.description)
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm, dry_run=dry_run)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    battery = tmp_path / "power_supply" / "BAT1"
    battery.mkdir(parents=True)
    (battery / "capacity").write_text("80\n")
    (battery / "status").write_text("Discharging\n")
    talks = Talks(None)
    talks.add(
        "find out how kestrels hunt",
        "Kestrels hover and drop onto voles. (from birds.example)",
        at(11, 14, 2),
    )
    talks.add("open kitty on 2", "Done, kitty's on 2.", at(12, 9, 15))
    yield Rig(
        ToolContext(
            await HyprlandDesktop.connect(fake_hypr.instance),
            AppResolver(AppCatalog(())),
            Settings(),
            which=lambda name: None,
            system=SystemControl(FakeSystem(), power_supply=tmp_path / "power_supply", home=tmp_path),
            talks=talks,
            now=lambda: NOW,
        )
    )
    talks.close()


@pytest.mark.anyio
async def test_each_request_is_kept_with_what_sonu_said(rig: Rig) -> None:
    talks = rig.ctx.talks
    assert talks is not None
    report = await rig.run("what's my battery?", ("system.status", {"what": ["battery"]}))
    assert report.status == "done"
    kept = talks.between(0, NOW.timestamp() + 3600)[-1]
    assert kept.request == "what's my battery?"
    assert "80%" in kept.reply  # a private result the user heard is kept as they heard it

    before = talks.count()
    await rig.run("what's my battery now?", ("system.status", {"what": ["battery"]}), dry_run=True)
    await rig.run("what did we talk about today?", ("conversations.recall", {"when": "today"}))
    assert talks.count() == before  # a dry run, and looking back, aren't kept


@pytest.mark.anyio
async def test_what_did_we_talk_about(rig: Rig) -> None:
    report = await rig.run("what did we talk about today?", ("conversations.recall", {"when": "today"}))
    outcome = report.steps[0].outcome
    assert outcome.private
    assert outcome.summary == "Today you asked me 1 thing: 09:15 “open kitty on 2” (Done, kitty's on 2.)"

    report = await rig.run("what did I ask you yesterday?", ("conversations.recall", {"when": "yesterday"}))
    assert report.steps[0].outcome.summary.startswith("Yesterday you asked me 1 thing: 14:02 “find out how")


@pytest.mark.anyio
async def test_what_did_you_tell_me_about(rig: Rig) -> None:
    report = await rig.run(
        "what did you tell me about kestrels?", ("conversations.recall", {"about": "kestrels"})
    )
    assert report.steps[0].outcome.summary == (
        "Yesterday at 14:02 you asked “find out how kestrels hunt” — "
        "I said: Kestrels hover and drop onto voles. (from birds.example)"
    )
    report = await rig.run(
        "what did you tell me about falcons?", ("conversations.recall", {"about": "falcons"})
    )
    assert report.steps[0].outcome.summary == "We didn't talk about “falcons” in the last 30 days."


@pytest.mark.anyio
async def test_forgetting_asks_first(rig: Rig) -> None:
    report = await rig.run("forget our conversations", ("conversations.forget", {"since": "everything"}))
    assert report.status == "done"
    assert rig.asked == ["Delete all our conversations (2 requests) — it can't be undone"]
    assert rig.ctx.talks is not None and rig.ctx.talks.count() == 0


@pytest.mark.anyio
async def test_nothing_is_kept_when_its_off(rig: Rig) -> None:
    rig.ctx.talks = None
    report = await rig.run("what did we talk about today?", ("conversations.recall", {"when": "today"}))
    assert report.status == "failed" and "keep = false" in report.steps[0].outcome.summary


def test_what_sonu_told_the_user() -> None:
    from sonu.core.report import StepRecord
    from sonu.core.talks import told
    from sonu.tools.base import StepStatus, ToolOutcome

    def record(summary: str, *, private: bool) -> StepRecord:
        return StepRecord(0, "web.research", {}, "", ToolOutcome(StepStatus.DONE, summary, private=private))

    answer = "Kestrels hover and drop onto voles. (from birds.example)"
    plan = steps_plan(("web.research", {"query": "how kestrels hunt"}))
    report = TaskReport(
        "find out how kestrels hunt", plan, [record(answer, private=True)], reply="Here's what I found."
    )
    assert told(report) == f"Here's what I found. {answer}"  # the reply never holds a private result
    report = TaskReport(
        "open kitty", plan, [record("Opened kitty on workspace 2", private=False)], reply="Done!"
    )
    assert told(report) == "Done!"  # the result lines are only kept when there's no reply
    report = TaskReport("hi", Plan("reply", "Hi! What can I do?"))
    assert told(report) == "Hi! What can I do?"
