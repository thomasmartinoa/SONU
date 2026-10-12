"""Reading the user's own documents with the local model (D82): fake pdftotext and LibreOffice."""

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
from sonu.testing.fake_vision import FakePageReader
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


class Rig:
    def __init__(self, ctx: ToolContext, home: Path, system: FakeSystem, pages: FakePageReader) -> None:
        self.ctx, self.home, self.system, self.pages = ctx, home, system, pages

    async def run(self, request: str, args: dict[str, Any]) -> TaskReport:
        async def confirm(_: ConfirmRequest) -> bool:
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(("files.read", args))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


def make(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    system = FakeSystem()
    pages = FakePageReader()
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(),
        which=lambda name: None,
        system=SystemControl(system, home=home),
        pages=pages,
    )
    yield Rig(ctx, home, system, pages)


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_summarise_a_pdf_a_text_file_and_a_word_document(rig: Rig) -> None:
    make(rig.home / "Documents" / "invoice march.pdf", "Invoice 42: 120 euros, due 31 March.")
    report = await rig.run("summarise my invoice pdf", {"name": "invoice pdf"})
    assert report.status == "done", lines(report)
    assert rig.pages.calls[-1][:2] == ("Invoice 42: 120 euros, due 31 March.", "Summarise it.")
    assert rig.pages.whats[-1] == "the document 'invoice march.pdf'"
    assert report.steps[0].outcome.private
    assert report.steps[0].description == "Read 'invoice march.pdf' (~/Documents) with the local model"
    make(rig.home / "notes.txt", "Parking is 6 euros.")
    report = await rig.run(
        "what does notes.txt say about parking?", {"name": "notes.txt", "question": "what about parking?"}
    )
    assert report.status == "done" and rig.pages.calls[-1][:2] == (
        "Parking is 6 euros.",
        "what about parking?",
    )
    make(rig.home / "Documents" / "plan.docx", "Week 1: lentils.")
    report = await rig.run("summarise my plan docx", {"name": "plan docx"})
    assert report.status == "done", lines(report)
    assert rig.pages.calls[-1][0] == "Week 1: lentils."  # LibreOffice's byte-order mark gone
    [profile] = rig.system.office_profiles
    assert str(rig.home) not in profile and "sonu" in profile  # SONU's own profile, never the user's


@pytest.mark.anyio
async def test_what_isnt_read(rig: Rig) -> None:
    make(rig.home / "Pictures" / "lake.png", "x")
    report = await rig.run("summarise my lake png", {"name": "lake png"})
    assert lines(report) == ["SONU reads text files, PDFs and Word / OpenDocument files — not 'lake.png'"]
    make(rig.home / ".ssh" / "keys.txt", "secret")
    report = await rig.run("read ~/.ssh/keys.txt", {"name": "~/.ssh/keys.txt"})
    assert "hidden files" in lines(report)[0]
    make(rig.home / "empty.pdf", "   ")
    report = await rig.run("summarise empty.pdf", {"name": "empty.pdf"})
    assert lines(report) == ["'empty.pdf' has no text SONU can read (a scanned page is a picture)"]
    rig.system.installed.discard("pdftotext")
    make(rig.home / "Documents" / "report.pdf", "text")
    report = await rig.run("summarise report.pdf", {"name": "report.pdf"})
    assert lines(report) == ["pdftotext isn't installed (sudo pacman -S poppler)"]
    assert rig.pages.calls == []


@pytest.mark.anyio
async def test_a_long_document_says_only_its_start_was_read(rig: Rig) -> None:
    rig.pages.max_text = 50
    make(rig.home / "Documents" / "long.txt", "word " * 1000)
    report = await rig.run("summarise long.txt", {"name": "long.txt"})
    assert report.status == "done"
    text, _, partial, _ = rig.pages.calls[-1]
    assert len(text) == 50 and partial
    assert lines(report)[0].endswith("(SONU read the start of a long document.)")
