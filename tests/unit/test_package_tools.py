"""Installing software (M4.5): finding the package, explaining it, asking (high risk), installing in a
window the user can watch with the system's own password prompt, and checking it's installed."""

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
from sonu.integrations.kitty import SonuTerminals
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.system.packages import candidates, parse_info, parse_search, rank
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools import package_tools
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    confirmations: list[ConfirmRequest]
    picked: list[str]

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return approve

        async def choose(choices: Any) -> str | None:
            self.offered = [option.value for option in choices.options]
            return self.picked.pop(0) if self.picked else None

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm, choose=choose)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    fake_hypr.apps["kitty"] = FakeApp("kitty", "zsh")
    system = FakeSystem()
    fake_hypr.on_exec = system.kitty.launched
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog(entries=())),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=0.5)),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=tmp_path),
        terminals=SonuTerminals(tmp_path / "run" / "terminals", system),
    )
    monkeypatch.setattr(package_tools, "OFFICIAL_TIMEOUT_S", 5)
    monkeypatch.setattr(package_tools, "POLL_S", 0.02)
    yield Rig(fake_hypr, system, ctx, [], [])


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_install_from_the_official_repos(rig: Rig) -> None:
    report = await rig.run("install vlc", ("packages.install", {"name": "vlc"}))
    assert report.status == "done", lines(report)
    [asked] = rig.confirmations
    assert asked.risk is Risk.HIGH
    assert asked.description == (
        "Install vlc 3.0.21-1 from the official extra repo (Multi-platform MPEG, VCD/DVD, and DivX player, "
        "2.10 MiB to download). Your password is asked by the system itself."
    )
    assert asked.spoken == "install vlc"
    assert lines(report) == ["Installed vlc 3.0.21-1 from the extra repo"]
    # In a SONU window, through the system's own password prompt — never sudo.
    [program] = rig.system.kitty.programs.values()
    assert program.argv == ["pkexec", "pacman", "-S", "--needed", "--noconfirm", "--", "vlc"]
    assert program.state == "closed"  # SONU closes its install window when it worked
    assert not any("sudo" in part for call in rig.system.calls for part in call)


@pytest.mark.anyio
async def test_the_users_words_become_the_package_name(rig: Rig) -> None:
    report = await rig.run("install obs studio", ("packages.install", {"name": "obs studio"}))
    assert report.status == "done" and rig.system.installed_packages["obs-studio"] == "32.2.2-1"


@pytest.mark.anyio
async def test_several_packages_the_user_picks_official_first_then_the_aur(rig: Rig) -> None:
    rig.picked = ["aur/spotify"]
    report = await rig.run("install spotify", ("packages.install", {"name": "spotify"}))
    assert rig.offered == ["extra/spotify-launcher", "extra/spotify-player", "extra/spotifyd", "aur/spotify"]
    assert report.status == "done", lines(report)
    [asked] = rig.confirmations
    assert (
        "from the AUR" in asked.description
        and "built on this laptop from a script anyone can submit" in asked.description
    )
    assert asked.spoken == "install spotify from the AUR"
    [program] = rig.system.kitty.programs.values()
    assert program.argv[:2] == ["yay", "-S"] and program.argv[4:6] == ["--sudo", "pkexec"]


@pytest.mark.anyio
async def test_only_in_the_aur_it_still_asks_which(rig: Rig) -> None:
    report = await rig.run("install visual studio code", ("packages.install", {"name": "visual studio code"}))
    assert report.status == "needs-input" and rig.offered == ["aur/visual-studio-code-bin"]


@pytest.mark.anyio
async def test_nothing_is_installed_without_approval(rig: Rig) -> None:
    report = await rig.run("install vlc", ("packages.install", {"name": "vlc"}), approve=False)
    assert report.status == "declined" and "vlc" not in rig.system.installed_packages
    assert rig.system.kitty.programs == {}


@pytest.mark.anyio
async def test_a_cancelled_password_prompt_is_a_failure(rig: Rig) -> None:
    rig.system.polkit_answer = "dismiss"
    report = await rig.run("install vlc", ("packages.install", {"name": "vlc"}))
    assert report.status == "failed"
    assert lines(report) == [
        "vlc wasn't installed: the password prompt was cancelled (the window shows what happened)"
    ]
    [program] = rig.system.kitty.programs.values()
    assert program.state == "ready"  # left open, so the user can read why


@pytest.mark.anyio
async def test_already_installed(rig: Rig) -> None:
    report = await rig.run("install kitty", ("packages.install", {"name": "kitty"}))
    assert report.status == "done" and lines(report) == ["kitty is already installed (0.49.1-1)"]
    assert rig.confirmations == []


@pytest.mark.anyio
async def test_unknown_and_invented_names(rig: Rig) -> None:
    report = await rig.run("install flurbo", ("packages.install", {"name": "flurbo"}))
    assert report.status == "failed" and lines(report) == ["I couldn't find a package for 'flurbo'"]
    report = await rig.run("install a music player", ("packages.install", {"name": "rhythmbox"}))
    assert report.status == "failed" and "must be your words" in lines(report)[0]


# ---------------------------------------------------------------------------- reading pacman and yay
def test_reading_pacman_and_yay() -> None:
    # Copied from pacman 7 / yay 13 on the owner's laptop (LC_ALL=C).
    info = parse_info(
        "Repository      : extra\nName            : obs-studio\nVersion         : 32.2.2-1\n"
        "Description     : Free, open source software for live streaming and recording\n"
        "Download Size   : 6.69 MiB\nInstalled Size  : 24.71 MiB\n"
    )
    assert info is not None and (info.name, info.repo, info.size) == ("obs-studio", "extra", "6.69 MiB")
    found = parse_search(
        "aur/spotify 1:1.2.96.518-2 (+276 6.69) \n    A proprietary music streaming service\n"
        "extra/spotify-launcher 0.6.6-2 [installed]\n    Client for spotify's apt repository\n"
    )
    assert [(p.name, p.repo, p.votes) for p in found] == [
        ("spotify", "aur", 276),
        ("spotify-launcher", "extra", None),
    ]
    assert candidates("OBS Studio") == ["obs-studio", "obsstudio", "obs"]
    assert candidates("vlc; rm -rf /") == ["vlc-rm-rf", "vlcrmrf", "vlc"]  # only name characters survive
    assert candidates("-rf") == ["rf"]  # never a name pacman would read as an option
    from sonu.system.packages import valid_name

    assert not valid_name("-rf") and not valid_name("--config=/x") and valid_name("obs-studio")


def test_ranking() -> None:
    # Found on the laptop: pacman -Ss lists alphabetically — gst-plugin-spotify came first.
    found = parse_search(
        "extra/gst-plugin-spotify 0.14-1\n    Spotify source for GStreamer\n"
        "extra/spotify-launcher 0.6.6-2\n    Client for spotify's apt repository\n"
        "extra/spotifyd 0.4.2-1\n    Lightweight spotify streaming daemon\n"
    )
    assert [p.name for p in rank(found, "spotify")] == ["spotify-launcher", "spotifyd", "gst-plugin-spotify"]
