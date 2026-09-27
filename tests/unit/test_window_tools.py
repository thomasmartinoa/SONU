from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry
from sonu.tools.targets import wants_all


@dataclass
class Rig:
    fake: FakeHyprland
    ctx: ToolContext

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]]) -> TaskReport:
        async def approve(confirm_request: ConfirmRequest) -> bool:
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=approve)

    def window(self, window_class: str) -> dict[str, Any]:
        return self.fake.windows_of_class(window_class)[0]


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps.update({"kitty": FakeApp("kitty", "zsh"), "zen-browser": FakeApp("zen")})
    fake_hypr.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1", focus_history_id=0),
        make_window(
            "0xb2", "zen", "YouTube — Zen Browser", workspace_id=2, stable_id="b2", focus_history_id=1
        ),
        make_window("0xc3", "kitty", "btop", workspace_id=3, stable_id="c3", focus_history_id=2),
    ]
    fake_hypr.active_address = "0xa1"
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(catalog, role_defaults={"browser": "zen-browser", "terminal": "kitty"}),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=1.0)),
        which=lambda name: f"/usr/bin/{name}",
    )
    yield Rig(fake_hypr, ctx)


# ---------------------------------------------------------------- windows.state
@pytest.mark.anyio
async def test_fullscreen_and_back(rig: Rig) -> None:
    this = {"window": {"focused": True}}
    report = await rig.run("make this fullscreen", ("windows.state", {**this, "state": "fullscreen"}))
    assert report.lines() == ["✔ Made kitty fullscreen"]
    assert rig.window("kitty")["fullscreen"] == 2
    report = await rig.run("exit fullscreen", ("windows.state", {**this, "state": "normal"}))
    assert report.lines() == ["✔ Put kitty back to normal size"]
    assert rig.window("kitty")["fullscreen"] == 0


@pytest.mark.anyio
async def test_maximize_the_browser(rig: Rig) -> None:
    report = await rig.run(
        "maximize zen", ("windows.state", {"window": {"app": "zen"}, "state": "maximized"})
    )
    assert report.status == "done"
    assert rig.window("zen")["fullscreen"] == 1


@pytest.mark.anyio
async def test_pinning_floats_the_window_first(rig: Rig) -> None:
    # Hyprland only pins floating windows that aren't fullscreen (ConfigActions.cpp, pinWindow).
    rig.window("zen")["fullscreen"] = 2
    report = await rig.run("pin zen", ("windows.state", {"window": {"app": "zen"}, "state": "pinned"}))
    assert report.lines() == ["✔ Pinned zen (it floats and shows on every workspace)"]
    zen = rig.window("zen")
    assert (zen["fullscreen"], zen["floating"], zen["pinned"]) == (0, True, True)
    report = await rig.run("tile zen again", ("windows.state", {"window": {"app": "zen"}, "state": "tiled"}))
    assert report.status == "done"
    assert (zen["floating"], zen["pinned"]) == (False, False)


@pytest.mark.anyio
async def test_on_all_workspaces_is_not_all_windows(rig: Rig) -> None:
    # Found on real hardware (dry run): "pin kitty on all workspaces" would pin both kitty windows.
    report = await rig.run(
        "pin kitty on all workspaces", ("windows.state", {"window": {"app": "kitty"}, "state": "pinned"})
    )
    assert report.status == "needs-input"
    assert not any(w["pinned"] for w in rig.fake.windows)
    assert wants_all("close all the kitty windows") and wants_all("pin every kitty window on every workspace")
    assert not wants_all("keep zen on every desktop")


@pytest.mark.anyio
async def test_a_window_already_in_that_state_is_just_checked(rig: Rig) -> None:
    rig.window("zen")["floating"] = True
    report = await rig.run("float zen", ("windows.state", {"window": {"app": "zen"}, "state": "floating"}))
    assert report.lines() == ["✔ Made zen float"]
    assert not any("float" in request for request in rig.fake.requests)


@pytest.mark.anyio
async def test_a_state_that_never_shows_up_is_not_done(rig: Rig) -> None:
    # Dispatchers answer "ok" even when nothing changes (e.g. a window that refuses to float).
    rig.fake.ignore_state = True
    report = await rig.run(
        "float this", ("windows.state", {"window": {"focused": True}, "state": "floating"})
    )
    assert report.status != "done"
    assert report.lines() == ["✖ Couldn't make kitty floating"]


# ---------------------------------------------------------------- windows.side_by_side
@pytest.mark.anyio
async def test_side_by_side_on_the_first_windows_workspace(rig: Rig) -> None:
    rig.window("zen")["floating"] = True
    report = await rig.run(
        "put zen and kitty side by side",
        ("windows.side_by_side", {"first": {"app": "zen"}, "second": {"app": "kitty"}}),
    )
    assert report.lines() == ["✔ Put zen and kitty side by side on workspace 2"]
    zen, kitty = rig.fake.window("0xb2"), rig.fake.window("0xa1")  # the most recent kitty
    assert zen and kitty and zen["workspace"]["id"] == kitty["workspace"]["id"] == 2
    assert not zen["floating"]
    assert rig.fake.active_workspace == 2


@pytest.mark.anyio
async def test_two_windows_of_the_same_app(rig: Rig) -> None:
    report = await rig.run(
        "put my two terminals side by side",
        ("windows.side_by_side", {"first": {"app": "kitty"}, "second": {"app": "kitty"}}),
    )
    assert report.status == "done"
    assert sorted(w["workspace"]["id"] for w in rig.fake.windows_of_class("kitty")) == [1, 1]


@pytest.mark.anyio
async def test_only_one_window_is_not_a_pair(rig: Rig) -> None:
    report = await rig.run(
        "put zen and zen side by side",
        ("windows.side_by_side", {"first": {"app": "zen"}, "second": {"app": "zen"}}),
    )
    assert report.lines() == ["✖ There's only one zen window open"]
