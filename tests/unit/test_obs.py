from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.obs import (
    ObsClient,
    ObsError,
    ObsLink,
    ObsSettings,
    auth_answer,
    encode_frame,
    read_frame,
    read_settings,
)
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_obs import FakeObs, FakeObsState
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


# ---------------------------------------------------------------- the protocol
def test_auth_answer_matches_the_obs_websocket_docs() -> None:
    # The worked example from obs-websocket's protocol documentation (5.x).
    assert (
        auth_answer(
            "supersecretpassword",
            "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
            "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=",
        )
        == "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4="
    )


@pytest.mark.anyio
@pytest.mark.parametrize("size", [0, 125, 126, 65535, 65536])
async def test_frames_round_trip(size: int) -> None:
    payload = bytes(i % 251 for i in range(size))
    for mask in (True, False):
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(0x1, payload, mask=mask))
        reader.feed_eof()
        assert await read_frame(reader) == (True, 0x1, payload)


def test_settings_come_from_obs_and_never_show_the_password(tmp_path: Path) -> None:
    assert read_settings(tmp_path) is None
    path = tmp_path / ".config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"server_enabled": True, "server_port": 4455, "auth_required": True, "server_password": "hunter2"}
        )
    )
    settings = read_settings(tmp_path)
    assert settings == ObsSettings(enabled=True, port=4455, password="hunter2")
    assert "hunter2" not in repr(settings)


@pytest.mark.anyio
async def test_client_against_the_fake_server() -> None:
    async with FakeObs() as fake:
        async with ObsClient(fake.port, "secret") as obs:
            assert (await obs.request("GetSceneList"))["currentProgramSceneName"] == "Scene Main"
            with pytest.raises(ObsError, match="OBS refused StopRecord: Recording is not active"):
                await obs.request("StopRecord")
        with pytest.raises(ObsError, match="didn't accept SONU's password"):
            async with ObsClient(fake.port, "wrong"):
                pass


# ---------------------------------------------------------------- the tools
@dataclass
class Rig:
    fake_hypr: FakeHyprland
    obs: FakeObsState
    ctx: ToolContext
    home: Path
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
async def rig(fake_hypr: FakeHyprland, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    # The real /sys/module/v4l2loopback depends on the host (found after a reboot unloaded it).
    from sonu.tools import obs_tools

    monkeypatch.setattr(obs_tools, "V4L2LOOPBACK", tmp_path)
    home = tmp_path / "home"
    (home / "Videos").mkdir(parents=True)
    write_desktop_files(tmp_path / "share" / "applications")
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "share"), "XDG_DATA_DIRS": str(tmp_path / "x")}
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps["obs"] = FakeApp("com.obsproject.Studio", "OBS 32.2.2 - Profile: Untitled")
    state = FakeObsState(recordings_dir=home / "Videos")
    async with FakeObs(state) as fake:
        desktop = await HyprlandDesktop.connect(fake_hypr.instance)
        ctx = ToolContext(
            desktop,
            AppResolver(catalog),
            Settings(apps=AppsSettings(launch_timeout_s=3.0), verify=VerifySettings(timeout_s=0.5)),
            which=lambda name: f"/usr/bin/{name}",
            system=SystemControl(FakeSystem(), home=home),
            obs=ObsLink(settings=ObsSettings(enabled=True, port=fake.port, password="secret")),
        )
        yield Rig(fake_hypr, state, ctx, home)


@pytest.mark.anyio
async def test_record_stop_pause(rig: Rig) -> None:
    report = await rig.run("record my screen", ("obs.record", {"action": "start"}))
    assert report.lines() == ["✔ Recording in OBS — say 'stop recording' to save it"]
    assert rig.obs.recording
    report = await rig.run("pause the recording", ("obs.record", {"action": "pause"}))
    assert report.lines() == ["✔ Paused OBS's recording"] and rig.obs.paused
    report = await rig.run("resume", ("obs.record", {"action": "resume"}))
    assert report.lines() == ["✔ Resumed OBS's recording"]
    assert vars(rig.obs)["paused"] is False  # (read fresh: mypy narrowed .paused above)
    report = await rig.run("stop recording", ("obs.record", {"action": "stop"}))
    assert report.lines() == ["✔ Saved the recording to ~/Videos/2026-09-27 12-00-00.mkv"]
    report = await rig.run("stop recording", ("obs.record", {"action": "stop"}))
    assert report.lines() == ["✖ OBS isn't recording"]


@pytest.mark.anyio
async def test_switch_scenes_by_the_users_words(rig: Rig) -> None:
    report = await rig.run("switch obs to only screen", ("obs.scene", {"scene": "only screen"}))
    assert report.lines() == ["✔ OBS switched to 'Scene only screen'"]
    await rig.run("go to scene 3", ("obs.scene", {"scene": "scene 3"}))
    assert rig.obs.scene == "Scene 3"
    report = await rig.run("switch to gaming", ("obs.scene", {"scene": "gaming"}))
    assert report.lines() == [
        "✖ OBS has no scene like 'gaming' (scenes: Scene Main, Scene only screen, Scene 3)"
    ]


@pytest.mark.anyio
async def test_going_live_always_asks(rig: Rig) -> None:
    report = await rig.run("start streaming", ("obs.control", {"action": "stream_start"}), approve=False)
    assert report.status == "declined" and not rig.obs.streaming
    assert rig.asked[0].risk is Risk.HIGH
    report = await rig.run("start streaming", ("obs.control", {"action": "stream_start"}))
    assert report.lines() == ["✔ Started streaming in OBS"] and rig.obs.streaming


@pytest.mark.anyio
async def test_virtual_camera_and_replay_buffer(rig: Rig) -> None:
    await rig.run("turn on the virtual camera", ("obs.control", {"action": "virtualcam_start"}))
    assert rig.obs.virtualcam
    report = await rig.run("save a replay", ("obs.control", {"action": "replay_save"}))
    assert report.lines() == ["✖ OBS's replay buffer isn't running (say 'start the replay buffer')"]
    await rig.run("start the replay buffer", ("obs.control", {"action": "replay_start"}))
    report = await rig.run("save a replay", ("obs.control", {"action": "replay_save"}))
    assert report.lines() == ["✔ Saved the replay to ~/Videos/Replay 2026-09-27 12-00-00.mkv"]


@pytest.mark.anyio
async def test_mute_a_source(rig: Rig) -> None:
    report = await rig.run("mute my mic in obs", ("obs.mute", {"source": "mic", "mute": True}))
    assert report.lines() == ["✔ Muted 'Mic/Aux' in OBS"]
    assert rig.obs.inputs["Mic/Aux"] is True


@pytest.mark.anyio
async def test_obs_status(rig: Rig) -> None:
    rig.obs.recording = True
    report = await rig.run("is obs recording", ("system.status", {"what": ["obs"]}))
    assert report.lines() == [
        "✔ OBS: recording (00:00:05) · scene 'Scene Main' (also: Scene only screen, Scene 3) · "
        "not streaming · virtual camera off"
    ]


@pytest.mark.anyio
async def test_websocket_server_off(rig: Rig) -> None:
    rig.ctx.obs = ObsLink(settings=ObsSettings(enabled=False, port=4455, password=None))
    report = await rig.run("record my screen", ("obs.record", {"action": "start"}))
    assert report.lines() == [
        "✖ OBS's WebSocket server is off — turn it on once in OBS: Tools → WebSocket Server Settings → "
        "Enable WebSocket server"
    ]


@pytest.mark.anyio
async def test_recording_starts_obs_when_it_isnt_running(rig: Rig, tmp_path: Path) -> None:
    later = FakeObs(FakeObsState(recordings_dir=tmp_path))
    await later.start()
    port = later.port
    await later.__aexit__(None, None, None)  # nothing listens until OBS is "started"
    rig.ctx.obs = ObsLink(settings=ObsSettings(enabled=True, port=port, password="secret"))

    async def obs_comes_up() -> None:
        while ["obs"] not in rig.fake_hypr.launched:  # noqa: ASYNC110 — the fake has no launch event
            await asyncio.sleep(0.05)
        await later.start(port)

    task = asyncio.create_task(obs_comes_up())
    report = await rig.run("record my screen", ("obs.record", {"action": "start"}))
    await task
    assert report.status == "done"
    assert later.state.recording
    await later.__aexit__(None, None, None)
    report = await rig.run("switch to scene 3", ("obs.scene", {"scene": "scene 3"}))
    assert report.lines() == ["✖ OBS isn't running (say 'open OBS' first)"]


# ---------------------------------------------------------------- found on the owner's laptop
@pytest.mark.anyio
async def test_hybrid_mp4_cant_pause_and_says_why(rig: Rig) -> None:
    rig.obs.record_format = "mp4_output"
    await rig.run("record", ("obs.record", {"action": "start"}))
    report = await rig.run("pause the recording", ("obs.record", {"action": "pause"}))
    assert report.lines() == [
        "✖ OBS didn't pause the recording — OBS can't pause Hybrid MP4 recordings "
        "(OBS → Settings → Output → Recording Format: MKV or MP4 can pause)"
    ]


@pytest.mark.anyio
async def test_virtual_camera_without_v4l2loopback(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sonu.tools import obs_tools

    # Found on the owner's laptop: without the module OBS runs `pkexec modprobe …`, a password prompt
    # appears, and OBS ignores every later request until it's answered. SONU doesn't start it then.
    monkeypatch.setattr(obs_tools, "V4L2LOOPBACK", tmp_path / "no-such-module")
    report = await rig.run("virtual camera on", ("obs.control", {"action": "virtualcam_start"}))
    assert report.lines()[0].startswith("✖ OBS's virtual camera needs the v4l2loopback kernel module")
    assert "StartVirtualCam" not in rig.obs.requests


@pytest.mark.anyio
async def test_a_dialog_obs_waits_on_is_named(rig: Rig) -> None:
    # Found on the owner's laptop: OBS answered "ok" to StartRecord while a dialog was open, and
    # recorded only after it was closed.
    rig.fake_hypr.windows.append(
        make_window("0xd1", "com.obsproject.Studio", "Failed to start virtual camera", floating=True)
    )
    rig.obs.stuck = True
    report = await rig.run("record my screen", ("obs.record", {"action": "start"}))
    assert report.lines() == [
        "✖ OBS didn't start recording — OBS is probably waiting on 'Failed to start virtual camera': "
        "answer or close it (OBS then does what was asked)"
    ]


@pytest.mark.anyio
async def test_replay_buffer_off_in_settings(rig: Rig) -> None:
    rig.obs.replay_available = False
    for action in ("replay_start", "replay_save"):
        report = await rig.run("replay", ("obs.control", {"action": action}))
        assert report.lines() == [
            "✖ OBS's replay buffer is off — turn it on in OBS: Settings → Output → Replay Buffer → "
            "Enable Replay Buffer"
        ]
