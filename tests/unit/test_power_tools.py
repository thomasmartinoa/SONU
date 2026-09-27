from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import AutonomyMode, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.planner import _PlanModel, _problems
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    asked: list[ConfirmRequest] = field(default_factory=list)

    async def run(
        self,
        request: str,
        step: tuple[str, dict[str, Any]],
        *,
        approve: bool,
        autonomy: AutonomyMode = "confirm-risky",
    ) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.asked.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(step)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy(autonomy),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    fake_hypr.windows = [
        make_window("0xa1", "kitty", "zsh", focus_history_id=0),
        make_window("0xa2", "kitty", "btop", focus_history_id=1),
        make_window("0xb2", "zen", "Private page title", focus_history_id=2),
    ]
    system = FakeSystem()
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog(())),
        Settings(verify=VerifySettings(timeout_s=0.5)),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=tmp_path),
    )
    yield Rig(fake_hypr, system, ctx)


@pytest.mark.anyio
@pytest.mark.parametrize("autonomy", ["confirm-risky", "trusted"])
async def test_shutting_down_always_asks_and_names_the_open_windows(rig: Rig, autonomy: AutonomyMode) -> None:
    # Found on real hardware: the terminal SONU runs in was left out of the list, but it closes too.
    rig.ctx.desktop.origin_address = "0xa1"
    report = await rig.run(
        "shut down", ("system.power", {"action": "shutdown"}), approve=False, autonomy=autonomy
    )
    assert report.status == "declined"
    assert rig.system.power_requests == []
    (asked,) = rig.asked
    assert asked.risk is Risk.HIGH
    assert asked.description == (
        "Shut down the laptop — open windows will close, unsaved work is lost: kitty ×2, zen"
    )  # classes only: window titles are personal


@pytest.mark.anyio
async def test_suspend_is_requested_never_done(rig: Rig) -> None:
    report = await rig.run("go to sleep", ("system.power", {"action": "suspend"}), approve=True)
    assert rig.system.power_requests == ["suspend"]
    assert report.status != "done"  # SONU can't watch the laptop sleep
    assert report.lines() == ["▲ Suspending now (requested — SONU can't watch it happen)"]


@pytest.mark.anyio
async def test_a_sleep_lock_is_reported(rig: Rig) -> None:
    rig.system.inhibited_by = "Steam"
    report = await rig.run("suspend", ("system.power", {"action": "suspend"}), approve=True)
    assert report.status == "failed"
    assert 'Operation inhibited by "Steam"' in report.lines()[0]


@pytest.mark.anyio
async def test_log_out_ends_the_hyprland_session(rig: Rig) -> None:
    await rig.run("log out", ("system.power", {"action": "logout"}), approve=True)
    assert rig.fake.exited
    assert rig.system.power_requests == []


def test_power_must_be_the_last_step() -> None:
    plan = _PlanModel.model_validate(
        {
            "kind": "steps",
            "steps": [
                {"tool": "system.power", "args": {"action": "suspend"}},
                {"tool": "settings.volume", "args": {"target": "speaker", "mute": True}},
            ],
        }
    )
    assert _problems(plan, ToolRegistry(), "suspend and mute") == [
        "step 1 (system.power) must be the last step"
    ]
