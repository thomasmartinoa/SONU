"""GitHub (M4 part 3): listing pull requests with their checks, and merging one — only when it's
ready, only after approval, and done only when GitHub says it's merged."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.system.github import checks_state
from sonu.testing.fake_github import FakePr
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@dataclass
class Rig:
    system: FakeSystem
    ctx: ToolContext
    confirmations: list[ConfirmRequest]
    picked: list[str]

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return approve

        async def choose(choices: Any) -> str | None:
            return self.picked.pop(0) if self.picked else None

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm, choose=choose)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    system = FakeSystem()
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog(entries=())),
        Settings(verify=VerifySettings(timeout_s=0.5)),
        system=SystemControl(system, home=tmp_path),
    )
    yield Rig(system, ctx, [], [])


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_open_pull_requests_with_their_checks(rig: Rig) -> None:
    report = await rig.run("what are my open pull requests?", ("github.prs", {}))
    assert report.status == "done"
    assert lines(report) == [
        "2 open pull requests: hypr-dotfiles #5 “Night light fix” — checks failing · "
        "SONU #31 “M4 part 3: GitHub” — checks passing"
    ]
    report = await rig.run("any pull requests in sonu?", ("github.prs", {"repo": "sonu"}))
    assert lines(report) == ["1 open pull request in sonu: SONU #31 “M4 part 3: GitHub” — checks passing"]


@pytest.mark.anyio
async def test_merge_asks_high_risk_then_checks_github(rig: Rig) -> None:
    report = await rig.run("merge the pull request in SONU", ("github.merge", {"repo": "SONU"}))
    assert report.status == "done", lines(report)
    [asked] = rig.confirmations
    assert asked.risk is Risk.HIGH
    assert asked.description == (
        "Merge pull request SONU #31 “M4 part 3: GitHub” (3 files, +120 -8, checks passing) on GitHub"
    )
    assert asked.spoken == "merge pull request 31 in SONU"
    assert rig.system.github.merged == ["thomasmartinoa/SONU#31"]
    assert lines(report) == ["Merged SONU #31 on GitHub"]


@pytest.mark.anyio
async def test_nothing_is_merged_without_approval(rig: Rig) -> None:
    report = await rig.run(
        "merge the pull request in SONU", ("github.merge", {"repo": "SONU"}), approve=False
    )
    assert report.status == "declined" and rig.system.github.merged == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"checks": [("CI", "COMPLETED", "FAILURE")]}, "its checks are failing"),
        ({"checks": [("CI", "IN_PROGRESS", "")]}, "its checks are still running"),
        ({"draft": True}, "it's a draft"),
        ({"mergeable": "CONFLICTING"}, "it has conflicts"),
    ],
)
@pytest.mark.anyio
async def test_a_pull_request_that_isnt_ready_is_never_offered(
    rig: Rig, change: dict[str, Any], problem: str
) -> None:
    pr = rig.system.github.find("thomasmartinoa/SONU", 31)
    assert pr is not None
    for name, value in change.items():
        setattr(pr, name, value)
    report = await rig.run("merge the pull request in SONU", ("github.merge", {"repo": "SONU"}))
    assert report.status == "failed" and problem in lines(report)[0]
    assert rig.confirmations == [] and rig.system.github.merged == []


@pytest.mark.anyio
async def test_several_open_ones_sonu_asks_which(rig: Rig) -> None:
    rig.system.github.prs.append(FakePr("thomasmartinoa/SONU", 32, "Journal"))
    rig.picked = ["thomasmartinoa/SONU#32"]
    report = await rig.run("merge the pull request in sonu", ("github.merge", {"repo": "sonu"}))
    assert report.status == "done" and rig.system.github.merged == ["thomasmartinoa/SONU#32"]


@pytest.mark.anyio
async def test_a_number_the_user_didnt_say_isnt_used(rig: Rig) -> None:
    rig.system.github.prs.append(FakePr("thomasmartinoa/SONU", 32, "Journal"))
    # The model made up "32"; the user said no number — SONU asks which instead.
    report = await rig.run("merge the pull request in sonu", ("github.merge", {"repo": "sonu", "number": 32}))
    assert report.status == "needs-input" and rig.system.github.merged == []
    report = await rig.run("merge pull request 32 in sonu", ("github.merge", {"repo": "sonu", "number": 32}))
    assert report.status == "done" and rig.system.github.merged == ["thomasmartinoa/SONU#32"]


@pytest.mark.anyio
async def test_no_such_pull_request(rig: Rig) -> None:
    report = await rig.run("merge the pull request in flyhigh", ("github.merge", {"repo": "flyhigh"}))
    assert report.status == "failed" and lines(report) == ["There's no open pull request in flyhigh"]


def test_checks_count_each_check_once() -> None:
    # Found on the owner's repo: the same check shows twice (push and pull_request runs).
    rollup = [
        {"name": "Python 3.12", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "Python 3.12", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "GitGuardian Security Checks", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert checks_state(rollup) == "passing"
    assert checks_state(rollup[:1]) == "failing"
    assert checks_state([{"name": "CI", "status": "QUEUED", "conclusion": ""}]) == "running"
    assert checks_state([]) == "none"
