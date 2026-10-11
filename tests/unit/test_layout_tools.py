"""Saved layouts (D67): save which app is on which workspace, restore it, list and forget."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.layouts import Layout, Layouts, Slot
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, app_pid, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

WORK = Layout(
    "work",
    (
        Slot("kitty.desktop", "kitty", "1", "{home}/dev_works/SONU"),
        Slot("zen.desktop", "Zen Browser", "2"),
        Slot("code-oss.desktop", "Code - OSS", "3"),
    ),
)


@dataclass
class Rig:
    fake: FakeHyprland
    ctx: ToolContext
    home: Path
    layouts_file: Path
    confirmations: list[ConfirmRequest]

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)

    def window(self, window_class: str, workspace: int, title: str = "", address: str = "") -> None:
        address = address or f"0x{0x8000 + len(self.fake.windows):x}"
        self.fake.windows.append(
            make_window(
                address,
                window_class,
                title or window_class,
                workspace_id=workspace,
                pid=app_pid(window_class),
            )
        )

    def where(self) -> list[tuple[str, str]]:
        return sorted((w["class"], w["workspace"]["name"]) for w in self.fake.windows)

    def saved(self) -> Layouts:
        return Layouts.load(self.layouts_file)

    def save_work(self) -> None:
        slots = tuple(
            Slot(s.app, s.app_name, s.workspace, s.folder.format(home=self.home)) for s in WORK.slots
        )
        self.saved().put(Layout("work", slots))


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    for folder in ("dev_works/SONU", "notes"):
        (home / folder).mkdir(parents=True)
    write_desktop_files(tmp_path / "share" / "applications")
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "share"), "XDG_DATA_DIRS": str(tmp_path / "x")}
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps["kitty"] = FakeApp("kitty", "zsh")
    fake_hypr.apps["code-oss"] = FakeApp("code-oss", "Code - OSS", single_instance=True)
    fake_hypr.apps["zen-browser"] = FakeApp("zen", "Zen Browser")
    fake_hypr.proc = tmp_path / "proc"
    system = FakeSystem()
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    layouts_file = tmp_path / "data" / "layouts.json"
    ctx = ToolContext(
        desktop,
        AppResolver(catalog, role_defaults={"terminal": "kitty"}),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=0.5)),
        default_terminal="kitty",
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=home, proc=tmp_path / "proc"),
        layouts=Layouts(layouts_file),
    )
    yield Rig(fake_hypr, ctx, home, layouts_file, [])


def summaries(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


# ---------------------------------------------------------------------------- saving
@pytest.mark.anyio
async def test_save_the_whole_desktop(rig: Rig) -> None:
    rig.window("kitty", 1, "~/dev_works/SONU")
    rig.fake._shell_in(app_pid("kitty"), str(rig.home / "dev_works/SONU"))
    rig.window("zen", 2, "My bank — Zen Browser")
    rig.window("code-oss", 3)
    rig.window("kitty", 4, "SONU · Claude Code · SONU")  # SONU's own terminal: not saved
    rig.window("mystery-app", 4)  # no app SONU could start again
    report = await rig.run("save this layout as work", ("layouts.save", {"name": "work"}))
    assert report.status == "done", summaries(report)
    assert summaries(report) == [
        "Saved the layout 'work': kitty on 1, Zen Browser on 2, Code - OSS on 3 — with 1 terminal folder"
    ]
    layout = rig.saved().get("Work")
    assert layout is not None
    assert layout.slots == (
        Slot("kitty.desktop", "kitty", "1", str(rig.home / "dev_works/SONU")),
        Slot("zen.desktop", "Zen Browser", "2"),
        Slot("code-oss.desktop", "Code - OSS", "3"),
    )
    assert "bank" not in rig.layouts_file.read_text()  # never a title
    assert stat.S_IMODE(rig.layouts_file.stat().st_mode) == 0o600
    assert not rig.confirmations


@pytest.mark.anyio
async def test_save_only_this_workspace(rig: Rig) -> None:
    rig.window("kitty", 1)
    rig.window("zen", 2)
    rig.fake.monitors[0]["activeWorkspace"] = {"id": 2, "name": "2"}
    report = await rig.run("save this workspace as browsing", ("layouts.save", {"name": "browsing"}))
    assert summaries(report) == ["Saved the layout 'browsing': Zen Browser on 2"]


@pytest.mark.anyio
async def test_replacing_a_layout_asks_first(rig: Rig) -> None:
    rig.save_work()
    rig.window("zen", 5)
    report = await rig.run("save this layout as work", ("layouts.save", {"name": "work"}), approve=False)
    assert report.status == "declined"
    assert rig.confirmations[0].risk is Risk.MEDIUM
    assert "Replace the layout 'work' (kitty on 1, Zen Browser on 2, Code - OSS on 3)" in (
        rig.confirmations[0].description
    )
    layout = rig.saved().get("work")
    assert layout is not None
    assert len(layout.slots) == 3  # kept


@pytest.mark.anyio
async def test_the_name_must_be_the_users(rig: Rig) -> None:
    rig.window("kitty", 1)
    report = await rig.run("save this layout", ("layouts.save", {"name": "coding"}))
    assert report.status == "failed"
    assert rig.saved().items == []


# ---------------------------------------------------------------------------- restoring
@pytest.mark.anyio
async def test_restore_moves_what_is_open_and_opens_what_is_missing(rig: Rig) -> None:
    rig.save_work()
    rig.window("zen", 5, address="0x9001")
    rig.window("kitty", 1, address="0x9002")  # a terminal in another folder: left alone
    rig.fake._shell_in(app_pid("kitty"), str(rig.home / "notes"))
    report = await rig.run("restore my work layout", ("layouts.restore", {"name": "work"}))
    assert report.status == "done", summaries(report)
    assert summaries(report) == ["Restored the layout 'work': kitty on 1, Zen Browser on 2, Code - OSS on 3"]
    assert rig.where() == [("code-oss", "3"), ("kitty", "1"), ("kitty", "1"), ("zen", "2")]
    assert ["kitty", "--directory", str(rig.home / "dev_works/SONU")] in rig.fake.launched
    assert not any(argv[0].endswith("zen-browser") for argv in rig.fake.launched)  # moved, not opened again


@pytest.mark.anyio
async def test_restore_says_what_it_will_do(rig: Rig) -> None:
    rig.save_work()
    rig.window("zen", 2)
    report = await rig.run("restore work", ("layouts.restore", {"name": "work"}))
    assert report.plan is not None
    assert report.steps[0].outcome.status.value == "done"
    assert report.steps[0].description == (
        "Restore the layout 'work': open kitty in ~/dev_works/SONU on 1; open Code - OSS on 3; "
        "1 already in place"
    )


@pytest.mark.anyio
async def test_an_app_that_is_gone_is_reported(rig: Rig) -> None:
    rig.saved().put(
        Layout("old", (Slot("gone.desktop", "Gone App", "1"), Slot("zen.desktop", "Zen Browser", "2")))
    )
    report = await rig.run("restore old", ("layouts.restore", {"name": "old"}))
    assert report.status == "failed"
    assert summaries(report) == ["Restored the layout 'old' only in part: Gone App isn't installed anymore"]
    assert rig.where() == [("zen", "2")]


@pytest.mark.anyio
async def test_an_unknown_layout_lists_the_saved_ones(rig: Rig) -> None:
    rig.save_work()
    report = await rig.run("restore gaming", ("layouts.restore", {"name": "gaming"}))
    assert summaries(report) == ["There's no layout called 'gaming' — you have: work"]
    assert not rig.fake.launched


# ---------------------------------------------------------------------------- listing and forgetting
@pytest.mark.anyio
async def test_list_and_forget(rig: Rig) -> None:
    empty = await rig.run("what layouts do I have?", ("layouts.list", {}))
    assert summaries(empty) == ["You have no saved layouts"]
    rig.save_work()
    listed = await rig.run("what layouts do I have?", ("layouts.list", {}))
    assert summaries(listed) == ["Your layouts — work: kitty on 1, Zen Browser on 2, Code - OSS on 3"]
    forgot = await rig.run("forget the work layout", ("layouts.forget", {"name": "work layout"}))
    assert summaries(forgot) == ["Deleted the layout 'work'"]
    assert rig.saved().items == []


def test_a_broken_file_is_no_layouts(tmp_path: Path) -> None:
    path = tmp_path / "layouts.json"
    path.write_text("{not json")
    assert Layouts.load(path).items == []


@pytest.mark.anyio
async def test_this_layout_is_the_whole_desktop(rig: Rig) -> None:
    """Found on the laptop (dry run): "save this setup as coding" was planned as this workspace only."""
    rig.window("kitty", 1)
    rig.window("zen", 2)
    rig.fake.monitors[0]["activeWorkspace"] = {"id": 2, "name": "2"}
    report = await rig.run("remember this setup as coding", ("layouts.save", {"name": "coding"}))
    assert summaries(report) == ["Saved the layout 'coding': kitty on 1, Zen Browser on 2"]
