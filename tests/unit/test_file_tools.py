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
    assert find_folders("dev_works/SONU", home) == [home / "dev_works" / "SONU"]  # a path from home
    assert find_folders("dev_works/nothing", home) == []
    assert find_folders("dev_works/../../..", home) == []  # still only inside the home folder
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
    assert report.lines() == ["✖ There's no folder or file called 'recipes' in your home"]
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


@pytest.mark.anyio
async def test_last_download_in_the_request_even_if_the_place_dropped_it(rig: Rig) -> None:
    # Found in the evals (v31): planned as {"place": "my downloads"} — the folder opened, not the file.
    touch(rig.home / "Downloads" / "cat.png")
    report = await rig.run("open my last download", ("files.open", {"place": "my downloads"}))
    assert report.lines() == ["✔ Opened 'cat.png' in imv"]


# ---------------------------------------------------------------------------- a file by its name (D74)
@pytest.mark.anyio
async def test_open_a_file_by_its_name(rig: Rig) -> None:
    touch(rig.home / "Pictures" / "kestrel lake.png", age_s=500)
    touch(rig.home / "dev_works" / "node_modules" / "kestrel-lake.png")  # dependencies aren't searched
    report = await rig.run("open my kestrel lake picture", ("files.open", {"place": "kestrel lake picture"}))
    assert report.lines() == ["✔ Opened 'kestrel lake.png' in imv"]
    assert rig.fake.launched[-1] == ["imv", str(rig.home / "Pictures" / "kestrel lake.png")]


@pytest.mark.anyio
async def test_several_files_are_asked_about_and_programs_never_opened(rig: Rig) -> None:
    touch(rig.home / "Downloads" / "invoice march.png", age_s=100)
    touch(rig.home / "Pictures" / "invoice april.png", age_s=50)
    report = await rig.run("open the invoice", ("files.open", {"place": "invoice"}))
    assert report.status == "needs-input"
    outcome = report.steps[0].outcome
    assert outcome.private and outcome.choices is not None
    assert [o.label for o in outcome.choices.options] == [
        "invoice april.png — ~/Pictures",
        "invoice march.png — ~/Downloads",
    ]
    script = touch(rig.home / "Downloads" / "setup.sh")
    script.chmod(0o755)
    report = await rig.run("open setup.sh", ("files.open", {"place": "setup.sh"}))
    assert report.lines() == [
        "✖ 'setup.sh' is a program or script — SONU doesn't open those (it could run it)"
    ]
    assert len(rig.fake.launched) == 0


def test_file_words_and_kinds(tmp_path: Path) -> None:
    from sonu.tools.file_tools import file_words, files_named

    assert file_words("my invoice pdf") == (["invoice"], frozenset({".pdf"}))
    assert file_words("notes.txt") == (["notes", "txt"], None)
    touch(tmp_path / "a" / "notes.txt", age_s=10)
    touch(tmp_path / "b" / "old notes.txt", age_s=5)
    touch(tmp_path / ".hidden" / "notes.txt")
    # The file the words name exactly first, then the newest ones that contain them.
    assert [p.name for p in files_named(tmp_path, ["notes"], None)] == ["notes.txt", "old notes.txt"]


@pytest.mark.anyio
async def test_find_files_privately(rig: Rig) -> None:
    touch(rig.home / "Downloads" / "Invoice March.pdf", age_s=100)
    touch(rig.home / "Documents" / "invoice notes.txt", age_s=50)
    report = await rig.run("where is my invoice pdf?", ("files.find", {"name": "invoice pdf"}))
    outcome = report.steps[0].outcome
    assert report.status == "done" and outcome.private
    assert outcome.summary.startswith("1 file matches “invoice pdf”: Invoice March.pdf — ~/Downloads (")
    assert report.steps[0].description == "Look for files matching “invoice pdf”"
    report = await rig.run(
        "find invoice in documents", ("files.find", {"name": "invoice", "place": "documents"})
    )
    assert report.steps[0].outcome.summary.startswith(
        "1 file matches “invoice”: invoice notes.txt — ~/Documents"
    )
    report = await rig.run("find my tax return", ("files.find", {"name": "tax return"}))
    assert report.lines() == ["✔ No file matches “tax return”"]


@pytest.mark.anyio
async def test_recent_files(rig: Rig) -> None:
    report_pdf = touch(rig.home / "Documents" / "report.pdf")
    gone = rig.home / "Documents" / "deleted.pdf"
    share = rig.home / ".local" / "share"
    share.mkdir(parents=True)
    used = 'modified="2026-09-19T18:02:42Z" visited="2026-09-19T18:02:42Z"'
    later = 'modified="2026-09-21T10:00:00Z" visited="2026-09-21T10:00:00Z"'
    okular = '<bookmark:application name="Okular" modified="2026-09-20T09:00:00Z" count="2"/>'
    (share / "recently-used.xbel").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xbel version="1.0" xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks">\n'
        f'<bookmark href="file://{report_pdf.as_posix()}" {used}><info><metadata>'
        f"<bookmark:applications>{okular}</bookmark:applications></metadata></info></bookmark>\n"
        f'<bookmark href="file://{gone.as_posix()}" {later}/>\n'
        "</xbel>\n"
    )
    report = await rig.run("what files did I open recently?", ("files.recent", {}))
    outcome = report.steps[0].outcome
    assert outcome.private
    assert outcome.summary.startswith("Files you opened: report.pdf — ~/Documents (")
    assert outcome.summary.endswith(", Okular)")  # the deleted file isn't listed
    report = await rig.run("what files did I open in the last hour?", ("files.recent", {"when": "last_hour"}))
    assert report.lines() == ["✔ No recent files in the last hour"]
