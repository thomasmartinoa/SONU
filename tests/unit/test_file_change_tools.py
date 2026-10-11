"""Moving, renaming, the trash and restoring (D75) — on a scratch home and a fake `gio`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.file_change_tools import new_name_for, protected
from sonu.tools.registry import ToolRegistry


class Rig:
    def __init__(self, ctx: ToolContext, home: Path, system: FakeSystem) -> None:
        self.ctx, self.home, self.system = ctx, home, system
        self.asked: list[ConfirmRequest] = []
        self.picks: list[str] = []

    async def run(self, request: str, tool: str, args: dict[str, Any], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.asked.append(confirm_request)
            return approve

        async def choose(choices: Any) -> str | None:
            return self.picks.pop(0) if self.picks else None

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan((tool, args))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm, choose=choose)


def make(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    for folder in ("Downloads", "Documents", "Pictures", "dev_works/notes", ".config/hypr"):
        (home / folder).mkdir(parents=True)
    (home / ".config" / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_DOCUMENTS_DIR="$HOME/Documents"\n'
        'XDG_PICTURES_DIR="$HOME/Pictures"\n'
    )
    system = FakeSystem(trash_dir=home / ".local" / "share" / "Trash")
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(),
        which=lambda name: None,  # never a real program
        system=SystemControl(system, home=home),
    )
    yield Rig(ctx, home, system)


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_move_a_file_asks_and_checks(rig: Rig) -> None:
    invoice = make(rig.home / "Downloads" / "invoice march.pdf")
    report = await rig.run(
        "move my invoice to documents", "files.move", {"name": "invoice", "to": "documents"}
    )
    assert report.status == "done", lines(report)
    [asked] = rig.asked
    assert asked.risk is Risk.MEDIUM
    assert asked.description == "Move the file 'invoice march.pdf' (~/Downloads) to ~/Documents"
    assert not invoice.exists() and (rig.home / "Documents" / "invoice march.pdf").read_text() == "x"
    assert lines(report) == ["Moved 'invoice march.pdf' to ~/Documents"]


@pytest.mark.anyio
async def test_never_overwrites(rig: Rig) -> None:
    make(rig.home / "Downloads" / "report.pdf", "new")
    make(rig.home / "Documents" / "report.pdf", "old")
    rig.picks = [f"src:{rig.home / 'Downloads' / 'report.pdf'}"]  # two are called report.pdf: the user picks
    report = await rig.run(
        "move report.pdf to documents", "files.move", {"name": "report.pdf", "to": "documents"}
    )
    assert lines(report) == ["~/Documents already has a 'report.pdf' — SONU never overwrites"]
    assert (rig.home / "Documents" / "report.pdf").read_text() == "old"
    report = await rig.run(
        "rename downloads/report.pdf to report",
        "files.rename",
        {"name": "Downloads/report.pdf", "new_name": "report"},
    )
    assert report.status == "failed"  # report.pdf is already its name


@pytest.mark.anyio
async def test_rename_keeps_the_extension(rig: Rig) -> None:
    notes = make(rig.home / "Documents" / "notes.txt")
    report = await rig.run(
        "rename notes.txt to ideas", "files.rename", {"name": "notes.txt", "new_name": "ideas"}
    )
    assert report.status == "done", lines(report)
    assert rig.asked[0].description == "Rename the file 'notes.txt' (~/Documents) to 'ideas.txt'"
    assert not notes.exists() and (rig.home / "Documents" / "ideas.txt").exists()
    report = await rig.run(
        "rename ideas to plans", "files.rename", {"name": "ideas", "new_name": "secret plans"}
    )
    assert report.status == "failed"  # "secret" isn't the user's word


def test_names_and_protected_places(tmp_path: Path) -> None:
    file = make(tmp_path / "a.pdf")
    assert new_name_for(file, "march invoice") == "march invoice.pdf"
    assert new_name_for(file, "march.docx") == "march.docx"
    for bad in ("../escape", ".hidden", "a/b", "  "):
        assert new_name_for(file, bad) is None
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / "Downloads").mkdir()
    assert protected(home, home) == "it's your home folder"
    assert protected(home / "Downloads", home) == "Downloads is one of your main folders"
    assert protected(home / ".config", home) == "hidden files and folders (settings, keys) are left alone"
    assert protected(tmp_path / "a.pdf", home) == "it isn't in your home folder"


@pytest.mark.anyio
async def test_delete_goes_to_the_trash_and_comes_back(rig: Rig) -> None:
    rig.ctx.which = lambda name: None
    download = make(rig.home / "Downloads" / "setup.zip")
    report = await rig.run("delete my last download", "files.trash", {"name": "my last download"})
    assert report.status == "done", lines(report)
    assert (
        rig.asked[0].description
        == "Move the file 'setup.zip' (~/Downloads) to the trash (it can be restored)"
    )
    assert not download.exists()
    assert lines(report) == ["Moved 'setup.zip' to the trash — say “restore setup.zip” to get it back"]
    assert ("gio", "trash", "--", str(download)) in rig.system.calls  # the trash, never rm
    report = await rig.run("restore setup.zip", "files.restore", {"name": "setup.zip"})
    assert report.status == "done", lines(report)
    assert download.read_text() == "x"
    assert lines(report) == ["Restored 'setup.zip' to ~/Downloads"]


@pytest.mark.anyio
async def test_a_folder_is_high_risk_and_main_folders_hidden_files_are_refused(rig: Rig) -> None:
    make(rig.home / "dev_works" / "notes" / "a.md")
    report = await rig.run("delete the notes folder", "files.trash", {"name": "notes folder"}, approve=False)
    assert report.status == "declined" and rig.asked[-1].risk is Risk.HIGH
    assert (rig.home / "dev_works" / "notes" / "a.md").exists()
    report = await rig.run("delete my downloads folder", "files.trash", {"name": "downloads"})
    assert lines(report) == ["SONU won't change 'Downloads': Downloads is one of your main folders"]
    report = await rig.run("delete ~/.config/hypr", "files.trash", {"name": "~/.config/hypr"})
    assert lines(report) == [
        "SONU won't change 'hypr': hidden files and folders (settings, keys) are left alone"
    ]
    assert (rig.home / ".config" / "hypr").is_dir()


@pytest.mark.anyio
async def test_several_matches_are_asked_once(rig: Rig) -> None:
    make(rig.home / "Downloads" / "invoice march.pdf")
    make(rig.home / "Pictures" / "invoice april.pdf")
    rig.picks = [f"src:{rig.home / 'Pictures' / 'invoice april.pdf'}"]
    report = await rig.run(
        "move the invoice to documents", "files.move", {"name": "invoice", "to": "documents"}
    )
    assert report.status == "done", lines(report)
    assert (rig.home / "Documents" / "invoice april.pdf").exists()
    assert (rig.home / "Downloads" / "invoice march.pdf").exists()
