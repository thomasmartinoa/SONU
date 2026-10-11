"""Local music with SONU's own mpv (D77) — a scratch home, the fake desktop and a fake mpv."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.mpv import SonuPlayer
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_mpv import FakeMpv
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

SONGS = (
    "Music/Kestrel Band/First Album/01 Morning.ogg",
    "Music/Kestrel Band/First Album/02 Ridge.ogg",
    "Music/Kestrel Band/Second Album/01 Lake.mp3",
    "Music/Other Artist/Single.flac",
    "Music/cover.jpg",
    "Downloads/podcast.mp3",
)


class Rig:
    def __init__(self, ctx: ToolContext, home: Path, mpv: FakeMpv, system: FakeSystem) -> None:
        self.ctx, self.home, self.mpv, self.system = ctx, home, mpv, system

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]]) -> TaskReport:
        async def confirm(_: ConfirmRequest) -> bool:
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path, short_tmp: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    for name in SONGS:
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_bytes(b"x")
    mpv = FakeMpv(short_tmp / "music")
    fake_hypr.on_exec = mpv.launched
    system = FakeSystem()
    system.installed.discard("playerctl")  # no other player: SONU's music needs none
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(verify=VerifySettings(timeout_s=1.0)),
        which=lambda name: None,
        system=SystemControl(system, home=home),
        music=SonuPlayer(short_tmp / "music"),
    )
    yield Rig(ctx, home, mpv, system)
    await mpv.aclose()


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_play_my_music_in_folder_order(rig: Rig) -> None:
    report = await rig.run("play my music", ("media.play_local", {}))
    assert report.status == "done", lines(report)
    assert report.steps[0].description == "Play 4 songs from ~/Music with mpv"
    assert [p.name for p in rig.mpv.songs] == ["01 Morning.ogg", "02 Ridge.ogg", "01 Lake.mp3", "Single.flac"]
    assert "--shuffle" not in rig.mpv.starts[0]
    assert lines(report) == [
        "Playing 4 songs from ~/Music: playing — 01 Morning — Kestrel Band (song 1 of 4)"
    ]
    assert report.steps[0].outcome.private


@pytest.mark.anyio
async def test_an_artist_shuffled_and_another_folder(rig: Rig) -> None:
    report = await rig.run(
        "shuffle kestrel band from my music", ("media.play_local", {"words": "kestrel band"})
    )
    assert report.status == "done", lines(report)
    assert len(rig.mpv.songs) == 3 and "--shuffle" in rig.mpv.starts[0]  # every album below the artist
    report = await rig.run("play the songs in my downloads", ("media.play_local", {"place": "downloads"}))
    assert report.status == "done", lines(report)
    assert [p.name for p in rig.mpv.songs] == ["podcast.mp3"]
    assert len(rig.mpv.starts) == 1  # the running mpv got the new list
    report = await rig.run("play polka from my music", ("media.play_local", {"words": "polka"}))
    assert lines(report) == ["No songs matching “polka” in ~/Music"]


@pytest.mark.anyio
async def test_pause_next_whats_playing_and_stop(rig: Rig) -> None:
    await rig.run("play my music", ("media.play_local", {}))
    report = await rig.run("pause the music", ("media.control", {"action": "pause"}))
    assert (lines(report), rig.mpv.paused) == (["Music paused"], True)
    report = await rig.run("what's playing?", ("system.status", {"what": ["media"]}))
    assert lines(report)[0].startswith(
        "Media (SONU's music): paused — 01 Morning — Kestrel Band (song 1 of 4)"
    )
    report = await rig.run("next song", ("media.control", {"action": "next"}))
    assert lines(report) == ["Skipped to the next song"] and rig.mpv.index == 1
    report = await rig.run("play", ("media.control", {"action": "play"}))
    assert (lines(report), rig.mpv.paused) == (["Music playing"], False)
    report = await rig.run("stop the music", ("media.control", {"action": "stop"}))
    assert lines(report) == ["Stopped the music"] and not rig.mpv.running
    assert rig.mpv.socket.exists()  # mpv leaves its socket file: SONU still knows it isn't running
    report = await rig.run("pause", ("media.control", {"action": "pause"}))
    assert report.status == "failed"  # nothing playing, no playerctl


@pytest.mark.anyio
async def test_no_music_folder_yet(rig: Rig, tmp_path: Path) -> None:
    rig.ctx.system.home = tmp_path / "empty"
    report = await rig.run("play my music", ("media.play_local", {}))
    assert lines(report) == ["No songs in ~/Music"]


@pytest.mark.anyio
async def test_stop_means_stop(rig: Rig) -> None:
    """Found on the laptop: "stop the music" was planned as pause."""
    await rig.run("play my music", ("media.play_local", {}))
    report = await rig.run("stop the music", ("media.control", {"action": "pause"}))
    assert lines(report) == ["Stopped the music"] and not rig.mpv.running
