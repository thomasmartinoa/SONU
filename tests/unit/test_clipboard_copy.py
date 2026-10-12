"""Copying to the clipboard by voice (D76) — a scratch home, a fake wl-copy, a fake browser."""

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
from sonu.integrations.browser import Browsers
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_browser import FakeBrowser, FakeTab
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import PNG, FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


class Rig:
    def __init__(self, ctx: ToolContext, home: Path, system: FakeSystem, browser: FakeBrowser) -> None:
        self.ctx, self.home, self.system, self.browser = ctx, home, system, browser

    async def run(self, request: str, args: dict[str, Any]) -> TaskReport:
        async def confirm(_: ConfirmRequest) -> bool:
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(("clipboard.copy", args))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


def make(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path, short_tmp: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    system = FakeSystem(mime_types={".png": "image/png", ".txt": "text/plain", ".pdf": "application/pdf"})
    fake_hypr.windows.append(make_window("0x20", "zen", "Kestrel Lake trail — Zen Browser", workspace_id=2))
    fake_hypr.active_address = "0x20"
    tabs = [FakeTab(1, "Kestrel Lake trail", "https://trails.example/kestrel", active=True)]
    async with FakeBrowser(short_tmp / "browsers", tabs=tabs) as browser:
        ctx = ToolContext(
            await HyprlandDesktop.connect(fake_hypr.instance),
            AppResolver(AppCatalog(())),
            Settings(),
            which=lambda name: None,  # never a real program
            system=SystemControl(system, home=home),
            browsers=Browsers(short_tmp / "browsers"),
        )
        yield Rig(ctx, home, system, browser)


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_copy_a_files_path(rig: Rig) -> None:
    invoice = make(rig.home / "Documents" / "invoice march.pdf")
    report = await rig.run("copy the path of my invoice", {"what": "path", "name": "invoice"})
    assert report.status == "done", lines(report)
    assert rig.system.clipboard_text == str(invoice)
    assert report.steps[0].outcome.private
    assert lines(report) == ["Copied the path of 'invoice march.pdf' (~/Documents)"]


@pytest.mark.anyio
async def test_copy_a_picture_and_a_text_file(rig: Rig) -> None:
    make(rig.home / "Pictures" / "kestrel lake.png", PNG)
    report = await rig.run("copy my kestrel lake photo", {"what": "file", "name": "kestrel lake photo"})
    assert report.status == "done", lines(report)
    assert rig.system.clipboard_type == "image/png"
    make(rig.home / "Documents" / "shopping.txt", b"oat milk\neggs\n")
    report = await rig.run("copy my shopping list", {"what": "file", "name": "shopping"})
    assert report.status == "done", lines(report)
    assert rig.system.clipboard_text == "oat milk\neggs\n"
    make(rig.home / "Documents" / "report.pdf")
    report = await rig.run("copy my report", {"what": "file", "name": "report"})
    assert lines(report) == [
        "SONU copies pictures and text files; for 'report.pdf' it can copy its path instead"
    ]


@pytest.mark.anyio
async def test_copy_the_newest_screenshot_and_the_pages_link(rig: Rig) -> None:
    make(rig.home / "Pictures" / "screenshot" / "shot.png", PNG)
    report = await rig.run("copy my last screenshot", {"what": "screenshot"})
    assert report.status == "done" and rig.system.clipboard_type == "image/png"
    report = await rig.run("copy this page's link", {"what": "link"})
    assert report.status == "done", lines(report)
    assert rig.system.clipboard_text == "https://trails.example/kestrel"
    assert report.steps[0].description == "Copy the link of the page in zen"  # the address isn't in it


@pytest.mark.anyio
async def test_only_the_users_words_and_never_hidden_files(rig: Rig) -> None:
    report = await rig.run("copy hello world", {"what": "text", "text": "hello world"})
    assert report.status == "done" and rig.system.clipboard_text == "hello world"
    report = await rig.run("copy hello world", {"what": "text", "text": "my password is hunter2"})
    assert report.status == "failed" and rig.system.clipboard_text == "hello world"
    make(rig.home / ".ssh" / "id_ed25519.txt", b"secret")
    report = await rig.run("copy ~/.ssh/id_ed25519.txt", {"what": "file", "name": "~/.ssh/id_ed25519.txt"})
    assert report.status == "failed"
    assert "hidden files" in lines(report)[0] and rig.system.clipboard_text == "hello world"


@pytest.mark.anyio
async def test_the_file_the_users_words_name(rig: Rig) -> None:
    """Found in the evals (v37): "copy the path of my invoice" was planned with no name."""
    invoice = make(rig.home / "Documents" / "invoice march.pdf")
    report = await rig.run("copy the path of my invoice to the clipboard", {"what": "path"})
    assert report.status == "done", lines(report)
    assert rig.system.clipboard_text == str(invoice)


@pytest.mark.anyio
async def test_words_left_out_of_the_plan_come_from_the_request(rig: Rig) -> None:
    """Found on the laptop (dry runs): "copy hello from sonu" had no text; "copy the path of the SONU readme"
    found nothing (README.md doesn't say SONU; its folder does)."""
    report = await rig.run("copy hello from sonu", {"what": "text"})
    assert report.status == "done" and rig.system.clipboard_text == "hello from sonu"
    readme = make(rig.home / "dev_works" / "SONU" / "README.md")
    make(rig.home / "dev_works" / "other" / "README.md")
    make(rig.home / "dev_works" / "SONU" / "docs" / "adr" / "README.md")  # deeper in SONU: not the one meant
    report = await rig.run("copy the path of the SONU readme", {"what": "path", "name": "the SONU readme"})
    assert report.status == "done", lines(report)
    assert rig.system.clipboard_text == str(readme)


def test_the_name_stops_at_the_next_step() -> None:
    """Found on the laptop: "copy the path of parking notes then paste it in somewhere" looked for a file
    named "parking notes then paste it in somewhere"."""
    from sonu.tools.clipboard_tools import _name_said

    assert _name_said("Copy the path of parking notes then paste it in somewhere") == "parking notes"
    assert _name_said("Copy the path of my room and paste it somewhere") == "my room"
    assert _name_said("copy hello from sonu, then open kitty") == "hello from sonu"
    assert _name_said("copy salt and pepper") == "salt and pepper"  # a plain "and" is part of it
