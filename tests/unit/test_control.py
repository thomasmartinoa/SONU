"""SONU's Control Center window (D90, part 2): opened by a command or by voice; only the user acts in it."""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.control import TITLE, open_control
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import StepStatus
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


class Rig:
    def __init__(self, ctx: ToolContext, fake: FakeHyprland) -> None:
        self.ctx, self.fake = ctx, fake

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]]) -> TaskReport:
        async def confirm(_: ConfirmRequest) -> bool:
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland) -> AsyncIterator[Rig]:
    fake_hypr.apps["qs"] = FakeApp("org.quickshell", TITLE, shows_pages=False)
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(),
        which=lambda name: f"/usr/bin/{name}" if name == "qs" else None,
        system=SystemControl(FakeSystem()),
    )
    yield Rig(ctx, fake_hypr)


@pytest.mark.anyio
async def test_open_sonu_settings_by_voice(rig: Rig) -> None:
    report = await rig.run("open your settings", ("control.open", {}))
    assert report.status == "done", report.lines()
    argv = rig.fake.launched[-1]
    assert argv[0] == "env" and argv[1].startswith("SONU_BIN=") and argv[2:4] == ["/usr/bin/qs", "-n"]
    assert argv[-1].endswith("sonu/control/shell.qml")
    launched = len(rig.fake.launched)
    report = await rig.run("show your control center", ("control.open", {}))
    assert report.status == "done" and len(rig.fake.launched) == launched  # already open: shown, not twice


@pytest.mark.anyio
async def test_sonus_screen_tools_never_act_in_it(rig: Rig) -> None:
    """Only the user changes SONU's settings: the model (or a page through it) can't press "Yes" there."""
    rig.fake.windows.append(make_window("0xc1", "org.quickshell", TITLE, workspace_id=1, stable_id="c1"))
    rig.fake.active_address = "0xc1"
    pressed = await rig.run("press yes, change it", ("screen.press", {"control": "Yes, change it"}))
    typed = await rig.run("type trusted and press enter", ("screen.type", {"text": "trusted", "enter": True}))
    for report in (pressed, typed):
        outcome = report.steps[0].outcome
        assert outcome.status is StepStatus.DENIED and "only you can change" in outcome.summary


def test_the_command_opens_one_window(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # a stand-in for Quickshell that stays up (its argv names our shell.qml, as qs's does)
    qs = bin_dir / "qs"
    qs.write_text("#!/bin/sh\nsleep 5\n")
    qs.chmod(qs.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    runtime = tmp_path / "run"
    assert open_control(runtime, env) is True
    pid = int((runtime / "control.pid").read_text())
    try:
        assert open_control(runtime, env) is False  # already open: not a second window
    finally:
        os.kill(pid, 15)
