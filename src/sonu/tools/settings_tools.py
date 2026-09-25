"""System settings: volume and mic, brightness, Bluetooth, Wi-Fi, media, power mode — and a read-only status.

Every change is read back and compared with what was asked; only a confirmed value is "done". The
programs are the ones the owner's rice uses (see ``sonu.system.control``). Arguments are typed
numbers and fixed words, so nothing from the model reaches a command line as free text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import AudioTarget, PlayerAction, PowerMode, Volume
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome, outcome_from_checks
from sonu.tools.context import ToolContext
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MIN_BRIGHTNESS = 5  # SONU never makes the screen (almost) black
TRACK_CHANGE_S = 3.0


async def _settle[T](read: Callable[[], Awaitable[T]], ok: Callable[[T], bool], timeout_s: float) -> T:
    """Read until the value is what we want or time runs out; return the last value read."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        value = await read()
        if ok(value) or loop.time() >= deadline:
            return value
        await asyncio.sleep(0.2)


def _missing(ctx: ToolContext, *programs: str) -> ToolOutcome | None:
    for program in programs:
        if not ctx.system.available(program):
            return ToolOutcome(StepStatus.FAILED, str(ProgramMissing(program)))
    return None


def _failed(exc: CommandError) -> ToolOutcome:
    return ToolOutcome(StepStatus.FAILED, str(exc))  # messages start with a program name or a capital


def _exactly_one(model: BaseModel, *names: str) -> None:
    given = [name for name in names if getattr(model, name) is not None]
    if len(given) != 1:
        raise ValueError(f"give exactly one of {', '.join(names)}")


# ---------------------------------------------------------------------------- settings.volume
class VolumeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: AudioTarget = Field(default="speaker", description="speaker (sound output) or mic")
    level: int | None = Field(default=None, ge=0, le=100, description="set to this percent")
    change: int | None = Field(
        default=None, ge=-100, le=100, description="raise (+) or lower (-) by this many percent; 'a bit' = 10"
    )
    mute: bool | None = Field(default=None, description="true to mute, false to unmute")

    @model_validator(mode="after")
    def _one_thing(self) -> Self:
        _exactly_one(self, "level", "change", "mute")
        return self


class SetVolume(Tool):
    name = "settings.volume"
    description = "Set, raise, lower, mute or unmute the speaker volume or the mic."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = VolumeArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(VolumeArgs, args)
        if problem := _missing(ctx, "wpctl"):
            return problem
        what = "the speaker volume" if a.target == "speaker" else "the mic volume"
        if a.mute is not None:
            text = f"{'Mute' if a.mute else 'Unmute'} the {a.target}"
        elif a.level is not None:
            text = f"Set {what} to {a.level}%"
        else:
            change = a.change or 0
            text = f"{'Raise' if change > 0 else 'Lower'} {what} by {abs(change)}%"
        return Prepared(text, a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(VolumeArgs, prepared.payload)
        timeout = ctx.settings.verify.timeout_s
        label = "Speaker" if a.target == "speaker" else "Mic"
        try:
            if a.mute is not None:
                await ctx.system.set_mute(a.target, a.mute)
                after = await _settle(
                    lambda: ctx.system.volume(a.target), lambda v: v.muted == a.mute, timeout
                )
                word = "muted" if a.mute else "unmuted"
                check = Check(f"{a.target} is {word}", after.muted == a.mute, {"muted": after.muted})
                return outcome_from_checks(
                    [check], f"{label} {word}", f"{label} is still {_mute_word(after)}"
                )
            before = await ctx.system.volume(a.target)
            wanted = a.level / 100 if a.level is not None else before.level + (a.change or 0) / 100
            wanted = round(max(0.0, min(wanted, 1.0)), 2)
            await ctx.system.set_volume(a.target, wanted)
            after = await _settle(
                lambda: ctx.system.volume(a.target), lambda v: abs(v.level - wanted) <= 0.011, timeout
            )
        except CommandError as exc:
            return _failed(exc)
        check = Check(
            f"{a.target} volume is {round(wanted * 100)}%",
            abs(after.level - wanted) <= 0.011,
            {"volume": after.level, "muted": after.muted},
        )
        note = f" — it's muted, so you won't hear it (say 'unmute the {a.target}')" if after.muted else ""
        return outcome_from_checks(
            [check],
            f"{label} volume is now {after.percent}%{note}",
            f"{label} volume is {after.percent}%, not {round(wanted * 100)}%",
        )


def _mute_word(volume: Volume) -> str:
    return "muted" if volume.muted else "unmuted"


# ---------------------------------------------------------------------------- settings.brightness
class BrightnessArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int | None = Field(default=None, ge=0, le=100, description="set screen brightness to this percent")
    change: int | None = Field(
        default=None,
        ge=-100,
        le=100,
        description="brighter (+) or dimmer (-) by this many percent; 'a bit' = 10",
    )

    @model_validator(mode="after")
    def _one_thing(self) -> Self:
        _exactly_one(self, "level", "change")
        return self


class SetBrightness(Tool):
    name = "settings.brightness"
    description = "Set the screen brightness, or make it brighter or dimmer."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = BrightnessArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(BrightnessArgs, args)
        if problem := _missing(ctx, "brightnessctl"):
            return problem
        if a.level is not None:
            return Prepared(f"Set the screen brightness to {max(a.level, MIN_BRIGHTNESS)}%", a)
        change = a.change or 0
        return Prepared(f"Make the screen {'brighter' if change > 0 else 'dimmer'} by {abs(change)}%", a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(BrightnessArgs, prepared.payload)
        try:
            before = await ctx.system.backlight()
            asked = a.level if a.level is not None else before.percent + (a.change or 0)
            wanted = max(MIN_BRIGHTNESS, min(asked, 100))
            await ctx.system.set_brightness(before.device, wanted)
            after = await _settle(
                ctx.system.backlight, lambda b: abs(b.percent - wanted) <= 1, ctx.settings.verify.timeout_s
            )
        except CommandError as exc:
            return _failed(exc)
        check = Check(
            f"screen brightness is {wanted}%",
            abs(after.percent - wanted) <= 1,
            {"device": after.device, "percent": after.percent},
        )
        note = (
            f" (SONU keeps at least {MIN_BRIGHTNESS}% so the screen stays visible)" if asked < wanted else ""
        )
        return outcome_from_checks(
            [check],
            f"Screen brightness is now {after.percent}%{note}",
            f"Screen brightness is {after.percent}%, not {wanted}%",
        )


# ---------------------------------------------------------------------------- settings.bluetooth
class SwitchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool = Field(description="true to turn it on, false to turn it off")


class SetBluetooth(Tool):
    name = "settings.bluetooth"
    description = "Turn Bluetooth on or off."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = SwitchArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SwitchArgs, args)
        if problem := _missing(ctx, "bluetoothctl"):
            return problem
        return Prepared(f"Turn Bluetooth {'on' if a.on else 'off'}", a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(SwitchArgs, prepared.payload)
        word = "on" if a.on else "off"
        try:
            if await ctx.system.bluetooth_powered() is None:
                return ToolOutcome(StepStatus.FAILED, "No Bluetooth adapter found")
            await ctx.system.set_bluetooth(a.on)
            powered = await _settle(
                ctx.system.bluetooth_powered, lambda p: p is a.on, ctx.settings.verify.timeout_s
            )
        except CommandError as exc:
            return _failed(exc)
        check = Check(f"Bluetooth is {word}", powered is a.on, {"powered": powered})
        return outcome_from_checks(
            [check],
            f"Bluetooth is {word}",
            f"Bluetooth didn't turn {word} (it may be blocked by a hardware switch)",
        )


# ---------------------------------------------------------------------------- settings.wifi
class SetWifi(Tool):
    name = "settings.wifi"
    description = "Turn Wi-Fi on or off."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = SwitchArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SwitchArgs, args)
        if problem := _missing(ctx, "nmcli"):
            return problem
        if a.on:
            return Prepared("Turn Wi-Fi on", a)
        # Turning Wi-Fi off can cut the connection (and a remote session): always ask first.
        return Prepared("Turn Wi-Fi off (this disconnects you from Wi-Fi)", a, risk=Risk.MEDIUM)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(SwitchArgs, prepared.payload)
        word = "on" if a.on else "off"
        try:
            await ctx.system.set_wifi(a.on)
            enabled = await _settle(
                ctx.system.wifi_enabled, lambda e: e is a.on, ctx.settings.verify.timeout_s
            )
        except CommandError as exc:
            return _failed(exc)
        check = Check(f"Wi-Fi is {word}", enabled is a.on, {"enabled": enabled})
        return outcome_from_checks([check], f"Wi-Fi is {word}", f"Wi-Fi didn't turn {word}")


# ---------------------------------------------------------------------------- media.control
class MediaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PlayerAction = Field(description="play, pause, toggle, next, previous or stop")


_EXPECTED_STATUS = {"play": "Playing", "pause": "Paused", "stop": "Stopped"}


class MediaControl(Tool):
    name = "media.control"
    description = "Play, pause, skip or go back in the music or video that is playing (any media player)."
    risk = Risk.LOW
    effects = frozenset({Effect.MEDIA_CONTROL})
    Args = MediaArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(MediaArgs, args)
        if problem := _missing(ctx, "playerctl"):
            return problem
        text = {
            "play": "Play the media",
            "pause": "Pause the media",
            "toggle": "Play/pause the media",
            "next": "Skip to the next track",
            "previous": "Go back to the previous track",
            "stop": "Stop the media",
        }[a.action]
        return Prepared(text, a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(MediaArgs, prepared.payload)
        timeout = ctx.settings.verify.timeout_s
        try:
            status = await ctx.system.player_status()
            if status is None:
                return ToolOutcome(StepStatus.FAILED, "No media player is running")
            if a.action in ("next", "previous"):
                track = await ctx.system.player_track()
                await ctx.system.player(a.action)
                after = await _settle(ctx.system.player_track, lambda t: t != track, TRACK_CHANGE_S)
                if after == track:
                    return ToolOutcome(
                        StepStatus.UNVERIFIED,
                        f"Asked the player for the {a.action} track, but it didn't report a different one",
                    )
                check = Check("the track changed", True, {"changed": True})  # titles stay out of the log
                done = (
                    "Skipped to the next track" if a.action == "next" else "Went back to the previous track"
                )
                return ToolOutcome(StepStatus.DONE, done, (check,))
            wanted = _EXPECTED_STATUS.get(a.action) or ("Paused" if status == "Playing" else "Playing")
            await ctx.system.player(a.action)
            after_status = await _settle(ctx.system.player_status, lambda s: s == wanted, timeout)
        except CommandError as exc:
            return _failed(exc)
        check = Check(f"the player is {wanted.lower()}", after_status == wanted, {"status": after_status})
        return outcome_from_checks(
            [check],
            f"Media {wanted.lower()}",
            f"The player is {str(after_status).lower()}, not {wanted.lower()}",
        )


# ---------------------------------------------------------------------------- settings.power_mode
class PowerModeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PowerMode = Field(description="performance, balanced or power-saver")


class SetPowerMode(Tool):
    name = "settings.power_mode"
    description = "Switch the power mode: performance, balanced or power-saver (battery saver)."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = PowerModeArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(PowerModeArgs, args)
        if problem := _missing(ctx, "powerprofilesctl"):
            return problem
        return Prepared(f"Switch the power mode to {a.mode}", a)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a = cast(PowerModeArgs, prepared.payload)
        try:
            await ctx.system.set_power_mode(a.mode)
            mode = await _settle(ctx.system.power_mode, lambda m: m == a.mode, ctx.settings.verify.timeout_s)
        except CommandError as exc:
            return _failed(exc)
        check = Check(f"power mode is {a.mode}", mode == a.mode, {"mode": mode})
        return outcome_from_checks([check], f"Power mode is {a.mode}", f"Power mode is {mode}, not {a.mode}")


# ---------------------------------------------------------------------------- system.status
StatusItem = Literal["volume", "mic", "brightness", "bluetooth", "wifi", "media", "power_mode", "battery"]
_ITEM_PROGRAM: dict[StatusItem, str | None] = {
    "volume": "wpctl",
    "mic": "wpctl",
    "brightness": "brightnessctl",
    "bluetooth": "bluetoothctl",
    "wifi": "nmcli",
    "media": "playerctl",
    "power_mode": "powerprofilesctl",
    "battery": None,
}


class StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: list[StatusItem] = Field(
        default_factory=list, max_length=8, description="which settings to report; empty = all of them"
    )


class SystemStatus(Tool):
    name = "system.status"
    description = (
        "Tell the user the current volume, mic, brightness, Bluetooth, Wi-Fi, media, power mode or battery."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = StatusArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        items = list(dict.fromkeys(cast(StatusArgs, args).what)) or list(_ITEM_PROGRAM)
        return Prepared("Check " + ", ".join(item.replace("_", " ") for item in items), items)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        parts = [await self._read(item, ctx) for item in cast(list[StatusItem], prepared.payload)]
        return ToolOutcome(StepStatus.DONE, " · ".join(part for part in parts if part))

    @staticmethod
    async def _read(item: StatusItem, ctx: ToolContext) -> str:
        program = _ITEM_PROGRAM[item]
        label = item.replace("_", " ").capitalize()
        if program and not ctx.system.available(program):
            return f"{label}: unknown ({program} isn't installed)"
        try:
            if item in ("volume", "mic"):
                volume = await ctx.system.volume("speaker" if item == "volume" else "mic")
                return f"{label}: {volume.percent}%{' (muted)' if volume.muted else ''}"
            if item == "brightness":
                return f"Brightness: {(await ctx.system.backlight()).percent}%"
            if item == "bluetooth":
                powered = await ctx.system.bluetooth_powered()
                return "Bluetooth: " + ("no adapter" if powered is None else "on" if powered else "off")
            if item == "wifi":
                return f"Wi-Fi: {'on' if await ctx.system.wifi_enabled() else 'off'}"
            if item == "media":
                status = await ctx.system.player_status()
                return f"Media: {status.lower() if status else 'nothing playing'}"
            if item == "power_mode":
                return f"Power mode: {await ctx.system.power_mode()}"
            batteries = ctx.system.batteries()
            if not batteries:
                return "Battery: none found"
            return " · ".join(f"Battery: {b.percent}% ({b.status.lower()})" for b in batteries)
        except CommandError as exc:
            return f"{label}: unknown ({exc})"


SETTINGS_TOOLS: tuple[Tool, ...] = (
    SetVolume(),
    SetBrightness(),
    SetBluetooth(),
    SetWifi(),
    MediaControl(),
    SetPowerMode(),
    SystemStatus(),
)
