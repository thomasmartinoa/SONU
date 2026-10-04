from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@dataclass
class Rig:
    system: FakeSystem
    ctx: ToolContext
    home: Path

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


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    backgrounds = home / ".config" / "orrery" / "current" / "theme" / "backgrounds"
    backgrounds.mkdir(parents=True)
    for name in ("1-mountain.png", "2-creation-of-adam.png"):
        (backgrounds / name).write_bytes(b"png")
    (home / "Pictures" / "screenshot").mkdir(parents=True)
    (home / "Pictures" / "beach sunset.jpg").write_bytes(b"jpg")
    (home / "Pictures" / "notes.txt").write_text("not a picture")
    system = FakeSystem(
        backgrounds=[str(backgrounds / "1-mountain.png"), str(backgrounds / "2-creation-of-adam.png")],
        wallpaper=str(backgrounds / "1-mountain.png"),
    )
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog(())),
        Settings(verify=VerifySettings(timeout_s=0.5)),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=home),
    )
    yield Rig(system, ctx, home)


# ---------------------------------------------------------------- desktop.theme
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("words", "theme"),
    [("tokyo", "tokyo-night"), ("catpuccin", "catppuccin-mocha"), ("hyprmono light", "hyprmono-light")],
)
async def test_theme_by_the_users_words(rig: Rig, words: str, theme: str) -> None:
    report = await rig.run(f"switch to {words}", ("desktop.theme", {"action": "set", "name": words}))
    assert report.lines() == [f"✔ Switched the theme to {theme}"]
    assert rig.system.theme == theme


@pytest.mark.anyio
async def test_an_exact_name_wins_and_a_vague_one_asks(rig: Rig) -> None:
    rig.system.theme = "tokyo-night"
    report = await rig.run("use hyprmono", ("desktop.theme", {"action": "set", "name": "hyprmono"}))
    assert rig.system.theme == "hyprmono" and report.status == "done"
    report = await rig.run("use mono", ("desktop.theme", {"action": "set", "name": "mono"}))
    assert report.status == "needs-input"
    assert report.questions == ["Which theme: hyprmono, hyprmono-light?"]


@pytest.mark.anyio
async def test_an_unknown_theme_lists_the_real_ones(rig: Rig) -> None:
    report = await rig.run("use dracula", ("desktop.theme", {"action": "set", "name": "dracula"}))
    assert report.lines() == [
        "✖ There's no theme like 'dracula' (themes: catppuccin-mocha, hyprmono, hyprmono-light, tokyo-night)"
    ]
    assert not any(call[:2] == ("orrery-theme", "set") for call in rig.system.calls)


@pytest.mark.anyio
async def test_light_and_dark_mode(rig: Rig) -> None:
    # Found in a dry run: "switch to light mode" was planned as "next theme".
    report = await rig.run("dark mode", ("desktop.theme", {"action": "dark"}))
    assert report.lines() == ["✔ Already using a dark theme (hyprmono)"]
    assert not any(call[:2] == ("orrery-theme", "toggle") for call in rig.system.calls)
    report = await rig.run("light mode", ("desktop.theme", {"action": "light"}))
    assert report.lines() == ["✔ Switched the theme to hyprmono-light"]
    report = await rig.run("dark mode", ("desktop.theme", {"action": "dark"}))
    assert rig.system.theme == "hyprmono" and report.status == "done"


@pytest.mark.anyio
async def test_a_theme_the_user_named_beats_next(rig: Rig) -> None:
    # Found in an eval: "switch to the tokyo night theme" was planned as {"action": "next"}.
    report = await rig.run("switch to the tokyo night theme", ("desktop.theme", {"action": "next"}))
    assert report.lines() == ["✔ Switched the theme to tokyo-night"]
    await rig.run("use hyprmono light", ("desktop.theme", {"action": "next"}))
    assert rig.system.theme == "hyprmono-light"  # the most specific name
    await rig.run("next theme please", ("desktop.theme", {"action": "next"}))
    assert rig.system.theme == "tokyo-night"  # no theme named: really the next one


@pytest.mark.anyio
async def test_a_theme_by_its_display_name(rig: Rig) -> None:
    report = await rig.run("use Tokyo Night", ("desktop.theme", {"action": "set", "name": "Tokyo Night"}))
    assert rig.system.theme == "tokyo-night" and report.status == "done"


@pytest.mark.anyio
async def test_next_theme_and_the_list(rig: Rig) -> None:
    report = await rig.run("next theme", ("desktop.theme", {"action": "next"}))
    assert report.lines() == ["✔ Switched the theme to hyprmono-light"]
    report = await rig.run("which theme and wallpaper", ("system.status", {"what": ["theme", "wallpaper"]}))
    assert report.lines() == [
        "✔ Theme: hyprmono-light (also: catppuccin-mocha, hyprmono, tokyo-night) · Wallpaper: 1-mountain.png"
    ]


# ---------------------------------------------------------------- desktop.wallpaper
@pytest.mark.anyio
async def test_next_wallpaper(rig: Rig) -> None:
    report = await rig.run("next wallpaper", ("desktop.wallpaper", {"action": "next"}))
    assert report.lines() == ["✔ The wallpaper is now 2-creation-of-adam.png"]


@pytest.mark.anyio
async def test_wallpaper_by_name_from_pictures(rig: Rig) -> None:
    report = await rig.run(
        "set my wallpaper to the beach sunset",
        ("desktop.wallpaper", {"action": "set", "name": "beach sunset"}),
    )
    assert report.lines() == ["✔ The wallpaper is now beach sunset.jpg"]
    assert rig.system.wallpaper == str(rig.home / "Pictures" / "beach sunset.jpg")


@pytest.mark.anyio
async def test_a_wallpaper_that_isnt_there(rig: Rig) -> None:
    report = await rig.run("wallpaper to notes", ("desktop.wallpaper", {"action": "set", "name": "notes"}))
    assert report.lines() == ["✖ There's no picture like 'notes' in the theme or ~/Pictures"]


# ---------------------------------------------------------------- clipboard
@pytest.mark.anyio
async def test_read_the_clipboard_privately(rig: Rig) -> None:
    report = await rig.run("what's on my clipboard", ("clipboard.read", {}))
    assert report.lines() == ["✔ The clipboard is empty"]
    rig.system.clipboard_text = "hunter2"
    report = await rig.run("what's on my clipboard", ("clipboard.read", {}))
    assert report.lines() == ["✔ The clipboard has: hunter2"]
    assert report.steps[0].outcome.private  # never in the audit log
    rig.system.clipboard_type, rig.system.clipboard_text = "image/png", None
    report = await rig.run("what's on my clipboard", ("clipboard.read", {}))
    assert report.lines() == ["✔ The clipboard has image/png (not text)"]


@pytest.mark.anyio
async def test_clear_the_clipboard(rig: Rig) -> None:
    rig.system.clipboard_text = "secret"
    report = await rig.run("clear my clipboard", ("clipboard.clear", {}))
    assert report.lines() == ["✔ Cleared the clipboard"]
    assert rig.system.clipboard_text is None


@pytest.mark.anyio
async def test_the_rice_renamed_orrery_and_its_old_names(rig: Rig) -> None:
    # Found on real hardware (2026-10-04): the rice was renamed Orrery; "switch to a light theme" said
    # "hypr-theme isn't installed". Its commands are orrery-theme / orrery-wall now.
    await rig.run("switch to tokyo", ("desktop.theme", {"action": "set", "name": "tokyo"}))
    report = await rig.run("next wallpaper", ("desktop.wallpaper", {"action": "next"}))
    assert report.status == "done"
    assert ("orrery-theme", "set", "tokyo-night") in rig.system.calls
    assert ("orrery-wall", "next") in rig.system.calls
    # A machine with the rice from before the rename still works.
    rig.system.installed -= {"orrery-theme", "orrery-wall"}
    rig.system.installed |= {"hypr-theme", "hypr-wall"}
    await rig.run("switch to hyprmono", ("desktop.theme", {"action": "set", "name": "hyprmono"}))
    assert ("hypr-theme", "set", "hyprmono") in rig.system.calls and rig.system.theme == "hyprmono"


@pytest.mark.anyio
async def test_next_wallpaper_when_the_theme_has_one(rig: Rig) -> None:
    # Found on real hardware: Orrery's Eclipse theme has a single background; "next wallpaper" changed
    # nothing and the reply only said it was still the same picture.
    (
        rig.home / ".config" / "orrery" / "current" / "theme" / "backgrounds" / "2-creation-of-adam.png"
    ).unlink()
    report = await rig.run("next wallpaper", ("desktop.wallpaper", {"action": "next"}))
    assert report.lines() == [
        "✖ This theme has only one wallpaper (1-mountain.png) — name a picture from ~/Pictures to use that "
        "instead"
    ]
    assert not any(call[:2] == ("orrery-wall", "next") for call in rig.system.calls)
