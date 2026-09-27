"""OBS Studio: record, pause, switch scenes, stream, the virtual camera, the replay buffer, mute.

Everything goes through OBS's own WebSocket server (``sonu.integrations.obs``, local only), and
every change is read back from OBS before it counts. Scenes and audio sources are picked by SONU
from OBS's real lists with the user's words. Starting a stream shows the screen to the world, so
it's high risk and always asks.

"Record my screen" starts OBS first if it isn't running; everything else needs OBS open. OBS's
WebSocket server is off by default: the user turns it on once (Tools → WebSocket Server Settings).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.hyprland.errors import HyprlandError
from sonu.integrations.obs import ObsClient, ObsError, ObsNotReachable
from sonu.tools.base import (
    Choice,
    Choices,
    Effect,
    Prepared,
    Risk,
    StepStatus,
    Tool,
    ToolOutcome,
    outcome_from_checks,
)
from sonu.tools.context import ToolContext
from sonu.tools.desktop_tools import resolve_app
from sonu.tools.rice_tools import pick_names
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_CHOICES = 6
_SCENE_FILLERS = {"scene", "the", "to", "my", "obs", "switch"}
_SOURCE_FILLERS = {"the", "my", "in", "obs", "audio", "source", "sound"}
TURN_ON = (
    "OBS's WebSocket server is off — turn it on once in OBS: Tools → WebSocket Server Settings → "
    "Enable WebSocket server"
)


def _home_relative(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def not_ready(ctx: ToolContext) -> ToolOutcome | None:
    """Why SONU can't talk to OBS at all (not installed, never started, server off), or None."""
    settings = ctx.obs.current()
    if settings is None:
        if not ctx.which("obs"):
            return ToolOutcome(
                StepStatus.FAILED, "OBS Studio isn't installed (install: sudo pacman -S obs-studio)"
            )
        return ToolOutcome(
            StepStatus.FAILED, "OBS hasn't been set up yet — open it once, then turn on its " + TURN_ON[5:]
        )
    if not settings.enabled:
        return ToolOutcome(StepStatus.FAILED, TURN_ON)
    return None


async def _connect(ctx: ToolContext, *, start: bool) -> ObsClient | ToolOutcome:
    """A connected client; if ``start``, open OBS first when it isn't running."""
    try:
        return await ctx.obs.client().__aenter__()
    except ObsNotReachable:
        if not start:
            return ToolOutcome(StepStatus.FAILED, "OBS isn't running (say 'open OBS' first)")
    except ObsError as exc:
        return ToolOutcome(StepStatus.FAILED, str(exc))
    entry = resolve_app("obs studio", ctx)
    if isinstance(entry, ToolOutcome):
        return entry
    argv = ctx.launch_argv(entry)
    if argv is None:
        return ToolOutcome(StepStatus.FAILED, f"{entry.name} can't be started")
    try:
        await ctx.desktop.launch(argv)
    except HyprlandError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Hyprland refused to start OBS: {exc}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.settings.apps.launch_timeout_s
    while True:
        await asyncio.sleep(0.5)
        try:
            return await ctx.obs.client().__aenter__()
        except ObsNotReachable:
            if loop.time() >= deadline:
                return ToolOutcome(
                    StepStatus.FAILED,
                    f"Started OBS, but it didn't answer within {ctx.settings.apps.launch_timeout_s:.0f}s",
                )
        except ObsError as exc:
            return ToolOutcome(StepStatus.FAILED, str(exc))


async def _until(
    read: Callable[[], Awaitable[dict[str, Any]]], ok: Callable[[dict[str, Any]], bool], ctx: ToolContext
) -> dict[str, Any]:
    """Re-read OBS until the state is what was asked for, or the verify timeout passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.settings.verify.timeout_s
    while True:
        state = await read()
        if ok(state) or loop.time() >= deadline:
            return state
        await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------- obs.record
class RecordArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop", "pause", "resume"]


class ObsRecord(Tool):
    name = "obs.record"
    description = (
        "Record the screen with OBS: start (opens OBS if needed), stop (saves the video), pause, resume."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SCREEN_CAPTURE, Effect.APP_LAUNCH})
    Args = RecordArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        action = cast(RecordArgs, args).action
        if problem := not_ready(ctx):
            return problem
        words = {
            "start": "Start recording in OBS",
            "stop": "Stop OBS's recording and save it",
            "pause": "Pause OBS's recording",
            "resume": "Resume OBS's recording",
        }
        return Prepared(words[action], action)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        action = cast(str, prepared.payload)
        obs = await _connect(ctx, start=action == "start")
        if isinstance(obs, ToolOutcome):
            return obs
        async with obs:
            try:
                return await self._record(obs, action, ctx)
            except ObsError as exc:
                return ToolOutcome(StepStatus.FAILED, str(exc))

    @staticmethod
    async def _record(obs: ObsClient, action: str, ctx: ToolContext) -> ToolOutcome:
        status = await obs.request("GetRecordStatus")
        active, paused = bool(status.get("outputActive")), bool(status.get("outputPaused"))

        def read() -> Awaitable[dict[str, Any]]:
            return obs.request("GetRecordStatus")

        if action == "start":
            if active:
                check = Check("OBS is recording", True, {"timecode": status.get("outputTimecode")})
                return outcome_from_checks([check], "OBS was already recording", "")
            await obs.request("StartRecord")
            now = await _until(read, lambda s: bool(s.get("outputActive")), ctx)
            check = Check("OBS is recording", bool(now.get("outputActive")), {})
            return outcome_from_checks(
                [check], "Recording in OBS — say 'stop recording' to save it", "OBS didn't start recording"
            )
        if not active:
            return ToolOutcome(StepStatus.FAILED, "OBS isn't recording")
        if action == "stop":
            saved = await obs.request("StopRecord")
            now = await _until(read, lambda s: not s.get("outputActive"), ctx)
            path = Path(str(saved.get("outputPath", "")))
            exists = bool(saved.get("outputPath")) and await asyncio.to_thread(
                lambda: path.is_file() and path.stat().st_size > 0
            )
            where = _home_relative(path, ctx.system.home)
            checks = [
                Check("OBS stopped recording", not now.get("outputActive"), {}),
                Check("the video was saved", exists, {"file": where}),
            ]
            return outcome_from_checks(
                checks, f"Saved the recording to {where}", "OBS's recording didn't save"
            )
        want = action == "pause"
        if paused == want:
            check = Check(f"the recording is {'paused' if want else 'running'}", True, {})
            return outcome_from_checks(
                [check], f"OBS's recording was already {'paused' if want else 'running'}", ""
            )
        await obs.request("PauseRecord" if want else "ResumeRecord")
        now = await _until(read, lambda s: bool(s.get("outputPaused")) == want, ctx)
        check = Check(
            f"the recording is {'paused' if want else 'running'}", bool(now.get("outputPaused")) == want, {}
        )
        return outcome_from_checks(
            [check],
            "Paused OBS's recording" if want else "Resumed OBS's recording",
            f"OBS didn't {action} the recording",
        )


# ---------------------------------------------------------------------------- obs.scene
class SceneArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene: str = Field(min_length=1, max_length=80, description="the scene as the user named it")


class ObsScene(Tool):
    name = "obs.scene"
    description = "Switch OBS to one of its scenes (by the user's words: 'only screen', 'scene 3')."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = SceneArgs
    grounded = ("scene",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SceneArgs, args)
        if problem := not_ready(ctx):
            return problem
        obs = await _connect(ctx, start=False)
        if isinstance(obs, ToolOutcome):
            return obs
        async with obs:
            try:
                listing = await obs.request("GetSceneList")
            except ObsError as exc:
                return ToolOutcome(StepStatus.FAILED, str(exc))
        scenes = [str(s["sceneName"]) for s in reversed(listing.get("scenes", [])) if "sceneName" in s]
        matches = [choice] if choice in scenes else pick_names(a.scene, scenes, _SCENE_FILLERS)
        if not matches:
            return ToolOutcome(
                StepStatus.FAILED, f"OBS has no scene like '{a.scene}' (scenes: {', '.join(scenes)})"
            )
        if len(matches) > 1:
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"several scenes match '{a.scene}'",
                question=f"Which scene: {', '.join(matches[:MAX_CHOICES])}?",
                choices=Choices(
                    "Which OBS scene?", tuple(Choice(name, name) for name in matches[:MAX_CHOICES])
                ),
            )
        return Prepared(f"Switch OBS to the scene '{matches[0]}'", matches[0])

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        scene = cast(str, prepared.payload)
        obs = await _connect(ctx, start=False)
        if isinstance(obs, ToolOutcome):
            return obs
        async with obs:
            try:
                await obs.request("SetCurrentProgramScene", {"sceneName": scene})
                now = await _until(
                    lambda: obs.request("GetSceneList"),
                    lambda s: s.get("currentProgramSceneName") == scene,
                    ctx,
                )
            except ObsError as exc:
                return ToolOutcome(StepStatus.FAILED, str(exc))
        current = now.get("currentProgramSceneName")
        check = Check(f"OBS shows '{scene}'", current == scene, {"scene": current})
        return outcome_from_checks([check], f"OBS switched to '{scene}'", f"OBS is still on '{current}'")


# ---------------------------------------------------------------------------- obs.control
ControlAction = Literal[
    "stream_start",
    "stream_stop",
    "virtualcam_start",
    "virtualcam_stop",
    "replay_start",
    "replay_stop",
    "replay_save",
]
# action → (request, status request, the state it should end in, what it's called)
_CONTROLS: dict[str, tuple[str, str, bool, str]] = {
    "stream_start": ("StartStream", "GetStreamStatus", True, "streaming"),
    "stream_stop": ("StopStream", "GetStreamStatus", False, "streaming"),
    "virtualcam_start": ("StartVirtualCam", "GetVirtualCamStatus", True, "the virtual camera"),
    "virtualcam_stop": ("StopVirtualCam", "GetVirtualCamStatus", False, "the virtual camera"),
    "replay_start": ("StartReplayBuffer", "GetReplayBufferStatus", True, "the replay buffer"),
    "replay_stop": ("StopReplayBuffer", "GetReplayBufferStatus", False, "the replay buffer"),
}


class ControlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ControlAction = Field(
        description="stream_start/stream_stop (go live), virtualcam_start/stop, replay_start/stop, "
        "replay_save (save the last moments)"
    )


class ObsControl(Tool):
    name = "obs.control"
    description = (
        "OBS streaming (going live asks first), the virtual camera, "
        "and the replay buffer (start, stop, save)."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SCREEN_CAPTURE})
    Args = ControlArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        action = cast(ControlArgs, args).action
        if problem := not_ready(ctx):
            return problem
        if action == "stream_start":
            # The screen goes out to the internet — always ask, whatever the autonomy mode.
            return Prepared(
                "Start streaming in OBS — your screen goes live to the service set up in OBS",
                action,
                risk=Risk.HIGH,
            )
        if action == "replay_save":
            return Prepared("Save OBS's replay buffer (the last moments) to a file", action)
        _, _, on, what = _CONTROLS[action]
        return Prepared(f"{'Start' if on else 'Stop'} {what} in OBS", action)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        action = cast(str, prepared.payload)
        obs = await _connect(ctx, start=False)
        if isinstance(obs, ToolOutcome):
            return obs
        async with obs:
            try:
                if action == "replay_save":
                    return await self._save_replay(obs, ctx)
                request, status_request, on, what = _CONTROLS[action]
                before = await obs.request(status_request)
                if bool(before.get("outputActive")) != on:
                    await obs.request(request)
                now = await _until(
                    lambda: obs.request(status_request), lambda s: bool(s.get("outputActive")) == on, ctx
                )
            except ObsError as exc:
                return ToolOutcome(StepStatus.FAILED, str(exc))
        state = "on" if on else "off"
        check = Check(f"{what} is {state}", bool(now.get("outputActive")) == on, {})
        verb = "Started" if on else "Stopped"
        return outcome_from_checks([check], f"{verb} {what} in OBS", f"OBS didn't turn {what} {state}")

    @staticmethod
    async def _save_replay(obs: ObsClient, ctx: ToolContext) -> ToolOutcome:
        if not (await obs.request("GetReplayBufferStatus")).get("outputActive"):
            return ToolOutcome(
                StepStatus.FAILED, "OBS's replay buffer isn't running (say 'start the replay buffer')"
            )
        await obs.request("SaveReplayBuffer")
        await asyncio.sleep(0.3)  # OBS writes the file in the background
        saved = str((await obs.request("GetLastReplayBufferReplay")).get("savedReplayPath", ""))
        path = Path(saved)
        exists = bool(saved) and await asyncio.to_thread(path.is_file)
        where = _home_relative(path, ctx.system.home)
        check = Check("the replay was saved", exists, {"file": where})
        return outcome_from_checks([check], f"Saved the replay to {where}", "OBS didn't save the replay")


# ---------------------------------------------------------------------------- obs.mute
class MuteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1, max_length=80, description="the OBS audio source as the user named it ('mic')"
    )
    mute: bool = True


class ObsMute(Tool):
    name = "obs.mute"
    description = "Mute or unmute one of OBS's audio sources (e.g. the mic or desktop audio in OBS)."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = MuteArgs
    grounded = ("source",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(MuteArgs, args)
        if problem := not_ready(ctx):
            return problem
        obs = await _connect(ctx, start=False)
        if isinstance(obs, ToolOutcome):
            return obs
        async with obs:
            try:
                inputs = [str(i["inputName"]) for i in (await obs.request("GetInputList")).get("inputs", [])]
            except ObsError as exc:
                return ToolOutcome(StepStatus.FAILED, str(exc))
        matches = [choice] if choice in inputs else pick_names(a.source, inputs, _SOURCE_FILLERS)
        if not matches:
            return ToolOutcome(
                StepStatus.FAILED, f"OBS has no source like '{a.source}' (sources: {', '.join(inputs)})"
            )
        if len(matches) > 1:
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"several OBS sources match '{a.source}'",
                question=f"Which source: {', '.join(matches[:MAX_CHOICES])}?",
                choices=Choices(
                    "Which OBS source?", tuple(Choice(name, name) for name in matches[:MAX_CHOICES])
                ),
            )
        return Prepared(f"{'Mute' if a.mute else 'Unmute'} '{matches[0]}' in OBS", (matches[0], a.mute))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        source, mute = cast(tuple[str, bool], prepared.payload)
        obs = await _connect(ctx, start=False)
        if isinstance(obs, ToolOutcome):
            return obs
        async with obs:
            try:
                await obs.request("SetInputMute", {"inputName": source, "inputMuted": mute})
                now = await _until(
                    lambda: obs.request("GetInputMute", {"inputName": source}),
                    lambda s: bool(s.get("inputMuted")) == mute,
                    ctx,
                )
            except ObsError as exc:
                return ToolOutcome(StepStatus.FAILED, str(exc))
        state = "muted" if mute else "on"
        check = Check(f"'{source}' is {state} in OBS", bool(now.get("inputMuted")) == mute, {})
        return outcome_from_checks(
            [check], f"{'Muted' if mute else 'Unmuted'} '{source}' in OBS", f"'{source}' didn't change in OBS"
        )


async def obs_status(ctx: ToolContext) -> str:
    """The OBS line for system.status: recording, scene, streaming, virtual camera (read-only)."""
    if (problem := not_ready(ctx)) is not None:
        return f"OBS: unavailable ({problem.summary})"
    try:
        async with ctx.obs.client() as obs:
            record = await obs.request("GetRecordStatus")
            scenes = await obs.request("GetSceneList")
            stream = await obs.request("GetStreamStatus")
            camera = await obs.request("GetVirtualCamStatus")
    except ObsNotReachable:
        return "OBS: not running"
    except ObsError as exc:
        return f"OBS: unknown ({exc})"
    if record.get("outputActive"):
        timecode = str(record.get("outputTimecode", "")).split(".")[0]
        recording = f"{'paused' if record.get('outputPaused') else 'recording'} ({timecode})"
    else:
        recording = "not recording"
    names = [str(s["sceneName"]) for s in reversed(scenes.get("scenes", [])) if "sceneName" in s]
    current = scenes.get("currentProgramSceneName")
    others = [n for n in names if n != current]
    parts = [
        f"OBS: {recording}",
        f"scene '{current}'" + (f" (also: {', '.join(others)})" if others else ""),
        "streaming" if stream.get("outputActive") else "not streaming",
        f"virtual camera {'on' if camera.get('outputActive') else 'off'}",
    ]
    return " · ".join(parts)


OBS_TOOLS: tuple[Tool, ...] = (ObsRecord(), ObsScene(), ObsControl(), ObsMute())
