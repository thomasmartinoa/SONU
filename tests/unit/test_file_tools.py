from __future__ import annotations

import os
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
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.file_tools import find_folders, newest_file
from sonu.tools.registry import ToolRegistry


@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    home: Path
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


def touch(path: Path, age_s: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = 1_790_000_000 - age_s
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_PICTURES_DIR="$HOME/Pictures"\nXDG_PROJECTS_DIR="$HOME/"\n'
    )
    for folder in (
        "Downloads",
        "Pictures/screenshot",
        "dev_works/SONU",
        "hypr-dotfiles/Screenshots",
        "notes",
    ):
        (home / folder).mkdir(parents=True)
    write_desktop_files(tmp_path / "share" / "applications")
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "share"), "XDG_DATA_DIRS": str(tmp_path / "x")}
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps.update({"thunar": FakeApp("Thunar", "Thunar"), "imv": FakeApp("imv", "imv")})
    system = FakeSystem()
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        # The rice's file manager is "thunar" — which is also the program of Thunar's Bulk Rename.
        AppResolver(catalog, role_defaults={"files": "thunar"}),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=0.5)),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=home),
    )
    yield Rig(fake_hypr, system, ctx, home, [])


# ---------------------------------------------------------------- finding folders (SONU, not the model)
def test_find_folders(tmp_path: Path) -> None:
    home = tmp_path
    for folder in ("Downloads", "Pictures/screenshot", "dev_works/SONU", "hypr-dotfiles/Screenshots"):
        (home / folder).mkdir(parents=True)
    (home / ".cache" / "downloads").mkdir(parents=True)  # hidden: never searched
    assert find_folders("my downloads", home) == [home / "Downloads"]
    assert find_folders("dev_works", home) == [home / "dev_works"]
    assert find_folders("dev works", home) == [home / "dev_works"]
    assert find_folders("the SONU folder", home) == [home / "dev_works" / "SONU"]
    assert find_folders("home", home) == [home]
    assert find_folders("~/dev_works/SONU", home) == [home / "dev_works" / "SONU"]
    assert find_folders("/etc", home) == []  # only folders in the user's home
    # "screenshots" is where SONU and the rice's keys save them; other matches only without it.
    assert find_folders("screenshots", home, home / "Pictures" / "screenshot") == [
        home / "Pictures" / "screenshot"
    ]
    assert find_folders("screenshots", home) == [
        home / "Pictures" / "screenshot",
        home / "hypr-dotfiles" / "Screenshots",
    ]
    assert find_folders("nothing like this", home) == []


def test_newest_file_skips_hidden_partial_and_folders(tmp_path: Path) -> None:
    touch(tmp_path / "old.pdf", age_s=100)
    touch(tmp_path / "photo.png", age_s=50)
    touch(tmp_path / "movie.mkv.part", age_s=1)  # still downloading
    touch(tmp_path / ".hidden", age_s=0)
    (tmp_path / "Telegram Desktop").mkdir()
    assert newest_file(tmp_path) == tmp_path / "photo.png"
    assert newest_file(tmp_path / "Telegram Desktop") is None


# ---------------------------------------------------------------- files.open
@pytest.mark.anyio
async def test_open_a_folder_in_the_rices_file_manager(rig: Rig) -> None:
    report = await rig.run("open my downloads", ("files.open", {"place": "downloads"}))
    assert report.lines() == ["✔ Opened ~/Downloads in Thunar File Manager"]
    # Found on real hardware: "thunar" also starts Bulk Rename (Exec=thunar --bulk-rename).
    assert rig.fake.launched[-1] == ["thunar", str(rig.home / "Downloads")]


@pytest.mark.anyio
async def test_several_folders_with_that_name_ask(rig: Rig) -> None:
    (rig.home / "dev_works" / "notes").mkdir()
    report = await rig.run("open notes", ("files.open", {"place": "notes"}))
    assert report.status == "needs-input"
    assert report.questions == ["Which one: ~/notes, ~/dev_works/notes?"]


@pytest.mark.anyio
async def test_a_folder_that_isnt_there(rig: Rig) -> None:
    report = await rig.run("open recipes", ("files.open", {"place": "recipes"}))
    assert report.lines() == ["✖ There's no folder called 'recipes' in your home"]
    assert rig.fake.launched == []


@pytest.mark.anyio
async def test_open_the_newest_download_with_its_app(rig: Rig) -> None:
    touch(rig.home / "Downloads" / "notes.zip", age_s=100)
    touch(rig.home / "Downloads" / "cat.png", age_s=10)
    report = await rig.run("open my last download", ("files.open", {"place": "downloads", "newest": True}))
    assert report.lines() == ["✔ Opened 'cat.png' in imv"]
    assert rig.fake.launched[-1] == ["imv", str(rig.home / "Downloads" / "cat.png")]
    assert rig.confirmations == []  # a picture: opened without asking
    assert report.steps[0].outcome.private  # file names stay out of the audit log


@pytest.mark.anyio
async def test_last_in_the_users_words_means_the_newest_file(rig: Rig) -> None:
    # Found in an eval: the model planned {"place": "my last download"} without newest.
    touch(rig.home / "Downloads" / "cat.png")
    report = await rig.run("open my last download", ("files.open", {"place": "my last download"}))
    assert report.lines() == ["✔ Opened 'cat.png' in imv"]


@pytest.mark.anyio
async def test_an_archive_asks_first(rig: Rig) -> None:
    touch(rig.home / "Downloads" / "game.zip")
    report = await rig.run(
        "open my last download", ("files.open", {"place": "downloads", "newest": True}), approve=False
    )
    assert report.status == "declined"
    assert len(rig.confirmations) == 1
    assert rig.fake.launched == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["install.sh", "app.desktop", "tool.bin"])
async def test_programs_and_scripts_are_never_opened(rig: Rig, name: str) -> None:
    path = touch(rig.home / "Downloads" / name)
    if name == "tool.bin":
        path.chmod(0o755)
    report = await rig.run("open my last download", ("files.open", {"place": "downloads", "newest": True}))
    assert report.status == "failed"
    assert "SONU doesn't open those (it could run it)" in report.lines()[0]
    assert rig.fake.launched == [] and rig.confirmations == []


@pytest.mark.anyio
async def test_a_type_nothing_opens(rig: Rig) -> None:
    touch(rig.home / "Downloads" / "paper.pdf")
    report = await rig.run("open my last download", ("files.open", {"place": "downloads", "newest": True}))
    assert report.lines() == ["✖ No app is set to open application/pdf files like 'paper.pdf'"]
