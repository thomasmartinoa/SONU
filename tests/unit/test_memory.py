from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.memory import Memory, MemoryItem
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.runtime import app_aliases
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


# ---------------------------------------------------------------- the store
def test_aliases_and_names_replace_notes_pile_up(tmp_path: Path) -> None:
    path = tmp_path / "data" / "memory.json"
    memory = Memory.load(path)
    memory.add(MemoryItem("alias", "vs code", "codium.desktop", "VSCodium"))
    memory.add(MemoryItem("alias", "vs code", "code-oss.desktop", "Code - OSS"))
    memory.add(MemoryItem("name", "Tom"))
    memory.add(MemoryItem("name", "Martin"))
    memory.add(MemoryItem("note", "the shop project is in dev_works/shop-app"))
    memory.add(MemoryItem("note", "I work late"))
    again = Memory.load(path)
    assert again.aliases() == {"vs code": "code-oss.desktop"}
    assert again.name() == "Martin"
    assert again.notes() == [
        "“vs code” means Code - OSS",
        "the shop project is in dev_works/shop-app",
        "I work late",
    ]
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_remembered_alias_wins_over_the_config() -> None:
    settings = Settings.model_validate(
        {"apps": {"aliases": {"vs code": "codium.desktop", "files": "thunar"}}}
    )
    memory = Memory(items=[MemoryItem("alias", "vs code", "code-oss.desktop", "Code - OSS")])
    assert app_aliases(settings, memory) == {"vs code": "code-oss.desktop", "files": "thunar"}


# ---------------------------------------------------------------- the tools
@dataclass
class Rig:
    ctx: ToolContext
    asked: list[ConfirmRequest] = field(default_factory=list)

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.asked.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")),
        Settings(),
        which=lambda name: f"/usr/bin/{name}",
        memory=Memory.load(tmp_path / "memory.json"),
    )
    yield Rig(ctx)


@pytest.mark.anyio
async def test_remember_what_an_app_name_means(rig: Rig) -> None:
    request = "remember when I say vs code I mean code oss"
    report = await rig.run(
        request, ("memory.remember", {"kind": "alias", "text": "VS Code", "app": "code oss"})
    )
    assert report.lines() == ["✔ I'll remember that “vs code” means Code - OSS"]
    assert Memory.load(rig.ctx.memory.path or Path()).aliases() == {"vs code": "code-oss.desktop"}
    # The next request's resolver (built by the runtime with app_aliases) opens Code - OSS for "VS Code".
    resolver = AppResolver(rig.ctx.resolver.catalog, aliases=app_aliases(Settings(), rig.ctx.memory))
    resolution = resolver.resolve("VS Code")
    assert resolution.entry is not None and resolution.entry.desktop_id == "code-oss.desktop"


@pytest.mark.anyio
async def test_the_users_full_app_name_wins_over_a_shortened_one(rig: Rig) -> None:
    # Found in an eval: "…I mean code oss" was planned with app "code" — several apps match that.
    request = "remember that when I say vs code I mean code oss"
    report = await rig.run(request, ("memory.remember", {"kind": "alias", "text": "vs code", "app": "code"}))
    assert report.lines() == ["✔ I'll remember that “vs code” means Code - OSS"]


@pytest.mark.anyio
async def test_an_app_that_isnt_installed_isnt_remembered(rig: Rig) -> None:
    report = await rig.run(
        "remember when I say music I mean tidal",
        ("memory.remember", {"kind": "alias", "text": "music", "app": "tidal"}),
    )
    assert report.status == "failed" and rig.ctx.memory.items == []


@pytest.mark.anyio
async def test_only_the_users_words_are_remembered(rig: Rig) -> None:
    report = await rig.run("remember my name", ("memory.remember", {"kind": "name", "text": "Martin"}))
    assert report.status == "failed" and "isn't in your request" in report.lines()[0]


@pytest.mark.anyio
async def test_name_note_list_and_forget(rig: Rig) -> None:
    await rig.run("call me Martin", ("memory.remember", {"kind": "name", "text": "Martin"}))
    await rig.run(
        "remember the shop project is in dev_works/shop-app",
        ("memory.remember", {"kind": "note", "text": "the shop project is in dev_works/shop-app"}),
    )
    report = await rig.run("what do you remember", ("memory.list", {}))
    assert report.lines() == ["✔ I remember: your name is Martin · the shop project is in dev_works/shop-app"]
    assert report.steps[0].outcome.private
    report = await rig.run("forget the shop thing", ("memory.forget", {"about": "shop"}))
    assert report.lines() == ["✔ Forgot the shop project is in dev_works/shop-app"]
    report = await rig.run("forget everything", ("memory.forget", {"all": True}), approve=False)
    assert report.status == "declined" and rig.asked[-1].risk is Risk.MEDIUM
    assert rig.ctx.memory.name() == "Martin"


@pytest.mark.anyio
async def test_an_alias_without_mean_is_a_note(rig: Rig) -> None:
    """Found in the evals (v33): a note about a folder was planned as an alias for an app."""
    report = await rig.run(
        "remember that the shop project is in dev_works/shop-app",
        ("memory.remember", {"kind": "alias", "text": "shop project", "app": "dev_works/shop-app"}),
    )
    assert report.lines() == ["✔ I'll remember: the shop project is in dev_works/shop-app"]
    assert [(i.kind, i.text) for i in rig.ctx.memory.items] == [
        ("note", "the shop project is in dev_works/shop-app")
    ]
