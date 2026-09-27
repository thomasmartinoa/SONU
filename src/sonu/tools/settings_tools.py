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

from sonu.desktop.apps.resolver import normalize
from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import AudioTarget, BluetoothDevice, PlayerAction, PowerMode, Volume
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
from sonu.tools.grounding import ungrounded_words
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

    # Required on purpose: with a default, the model once muted the speaker for "mute the mic"
    # (eval, 2026-09-25) — every step now has to say which one it means.
    target: AudioTarget = Field(description="speaker (sound, volume) or mic (microphone)")
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
    description = (
        "Play, pause, skip or go back in the media player that is already open. It cannot choose or "
        "search for a particular song, artist or video."
    )
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
StatusItem = Literal[
    "volume",
    "mic",
    "brightness",
    "bluetooth",
    "wifi",
    "media",
    "power_mode",
    "battery",
    "health",
    "cpu",
    "memory",
    "disk",
    "temperature",
    "gpu",
    "uptime",
]
_SETTINGS_ITEMS: tuple[StatusItem, ...] = (
    "volume",
    "mic",
    "brightness",
    "bluetooth",
    "wifi",
    "media",
    "power_mode",
    "battery",
)
_HEALTH_ITEMS: tuple[StatusItem, ...] = ("cpu", "memory", "temperature", "gpu", "disk", "uptime")
_ITEM_PROGRAM: dict[StatusItem, str | None] = {
    "volume": "wpctl",
    "mic": "wpctl",
    "brightness": "brightnessctl",
    "bluetooth": "bluetoothctl",
    "wifi": "nmcli",
    "media": "playerctl",
    "power_mode": "powerprofilesctl",
    "gpu": "nvidia-smi",
}
_GIB = 1024**3


class StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: list[StatusItem] = Field(
        default_factory=list,
        max_length=16,
        description="what to report; health = cpu, memory, temperature, gpu, disk, uptime; empty = settings",
    )


class SystemStatus(Tool):
    name = "system.status"
    description = (
        "Tell the user the current volume, mic, brightness, Bluetooth, Wi-Fi, media, power mode, battery, "
        "or the laptop's health: CPU, memory, temperature, GPU, disk space, uptime."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = StatusArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        asked = cast(StatusArgs, args).what or list(_SETTINGS_ITEMS)
        expanded = [part for item in asked for part in (_HEALTH_ITEMS if item == "health" else (item,))]
        items = list(dict.fromkeys(expanded))
        return Prepared("Check " + ", ".join(item.replace("_", " ") for item in items), items)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        parts = [await self._read(item, ctx) for item in cast(list[StatusItem], prepared.payload)]
        return ToolOutcome(StepStatus.DONE, " · ".join(part for part in parts if part))

    @staticmethod
    async def _read(item: StatusItem, ctx: ToolContext) -> str:
        program = _ITEM_PROGRAM.get(item)
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
                if not await ctx.system.wifi_enabled():
                    return "Wi-Fi: off"
                network = await ctx.system.wifi_active()
                return f"Wi-Fi: on ({network})" if network else "Wi-Fi: on (not connected)"
            if item == "media":
                status = await ctx.system.player_status()
                return f"Media: {status.lower() if status else 'nothing playing'}"
            if item == "power_mode":
                return f"Power mode: {await ctx.system.power_mode()}"
            if item in _HEALTH_ITEMS:
                return await _health(item, ctx)
            batteries = ctx.system.batteries()
            if not batteries:
                return "Battery: none found"
            return " · ".join(f"Battery: {b.percent}% ({b.status.lower()})" for b in batteries)
        except CommandError as exc:
            return f"{label}: unknown ({exc})"


async def _health(item: StatusItem, ctx: ToolContext) -> str:
    system = ctx.system
    if item == "cpu":
        percent = await system.cpu_percent()
        return "CPU: unknown" if percent is None else f"CPU: {percent:.0f}% busy"
    if item == "memory":
        memory = system.memory()
        return (
            "Memory: unknown"
            if memory is None
            else f"Memory: {memory[0] / _GIB:.1f} of {memory[1] / _GIB:.1f} GiB used"
        )
    if item == "disk":
        disk = system.disk()
        return (
            "Disk: unknown"
            if disk is None
            else f"Disk: {disk[0] / _GIB:.0f} of {disk[1] / _GIB:.0f} GiB free"
        )
    if item == "temperature":
        celsius = system.cpu_temperature()
        return "CPU temperature: unknown" if celsius is None else f"CPU temperature: {celsius:.0f} °C"
    if item == "gpu":
        gpu = await system.gpu()
        if gpu is None:
            return "GPU: unknown"
        util, celsius, used, total = gpu
        return f"GPU: {util}% busy · {celsius} °C · {used / 1024:.1f} of {total / 1024:.1f} GiB"
    seconds = system.uptime_s()
    if seconds is None:
        return "Uptime: unknown"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"Up {hours} h {minutes} min" if hours else f"Up {minutes} min"


# ---------------------------------------------------------------------------- settings.wifi_connect
CONNECT_VERIFY_S = 15.0
# Words that describe the kind of thing rather than name it ("my home wifi network").
_GENERIC = {
    "my",
    "the",
    "a",
    "an",
    "to",
    "named",
    "called",
    "wifi",
    "wi",
    "fi",
    "network",
    "bluetooth",
    "device",
}


def _ungrounded(text: str, ctx: ToolContext, what: str) -> ToolOutcome | None:
    missing = ungrounded_words(text, ctx.request_text)
    if missing:
        return ToolOutcome(
            StepStatus.FAILED, f"Won't use {what} '{text}': {', '.join(missing)} isn't in your request"
        )
    return None


def match_networks(words: str, saved: list[str]) -> list[str]:
    """Saved networks the user's words name: the whole name ("hi there" → "hithere") or every word."""
    wanted = normalize(words).replace(" ", "")
    exact = [name for name in saved if normalize(name).replace(" ", "") == wanted]
    if exact:
        return exact
    tokens = [token for token in normalize(words).split() if token not in _GENERIC]
    return [
        name
        for name in saved
        if tokens
        and all(t in normalize(name).split() or t in normalize(name).replace(" ", "") for t in tokens)
    ]


class WifiNetworkArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: str = Field(
        min_length=1, max_length=64, description="the Wi-Fi network's name as the user said it"
    )


class ConnectWifi(Tool):
    name = "settings.wifi_connect"
    description = "Connect to a Wi-Fi network by name (SONU checks that this computer knows it)."
    risk = Risk.LOW  # raised to medium when it would drop the current network
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = WifiNetworkArgs
    grounded = ("network",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(WifiNetworkArgs, args)
        if problem := _missing(ctx, "nmcli"):
            return problem
        try:
            if choice is not None:
                name = choice
            else:
                if problem := _ungrounded(a.network, ctx, "the network"):
                    return problem
                matches = match_networks(a.network, await ctx.system.wifi_saved())
                if not matches:
                    return ToolOutcome(
                        StepStatus.FAILED,
                        f"'{a.network}' isn't a saved Wi-Fi network — SONU can only connect to networks this "
                        "computer has joined before (a new one needs its password)",
                    )
                if len(matches) > 1:
                    return ToolOutcome(
                        StepStatus.NEEDS_INPUT,
                        f"several saved networks match '{a.network}'",
                        question=f"Which network did you mean: {' or '.join(matches[:4])}?",
                        choices=Choices("Which Wi-Fi network?", tuple(Choice(n, n) for n in matches[:6])),
                    )
                name = matches[0]
            active = await ctx.system.wifi_active()
        except CommandError as exc:
            return _failed(exc)
        if active == name:
            return Prepared(f"'{name}' is already connected", name)
        if active is not None:
            # Switching networks drops the current connection (and anything using it): ask first.
            return Prepared(f"Switch Wi-Fi from '{active}' to '{name}'", name, risk=Risk.MEDIUM)
        return Prepared(f"Connect to the Wi-Fi network '{name}'", name)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        name = cast(str, prepared.payload)
        try:
            if await ctx.system.wifi_active() != name:
                if not await ctx.system.wifi_enabled():
                    await ctx.system.set_wifi(True)
                    await _settle(ctx.system.wifi_enabled, bool, ctx.settings.verify.timeout_s)
                await ctx.system.connect_wifi(name)
            active = await _settle(ctx.system.wifi_active, lambda n: n == name, CONNECT_VERIFY_S)
        except CommandError as exc:
            return _failed(exc)
        check = Check(f"Wi-Fi is connected to '{name}'", active == name, {"connected": active == name})
        return outcome_from_checks([check], f"Connected to '{name}'", f"Couldn't connect to '{name}'")


# ---------------------------------------------------------------------------- settings.bluetooth_device
# Kind words → the start of bluez's "Icon:" for that kind of device.
_DEVICE_KINDS = {
    "headphones": "audio-head",
    "headphone": "audio-head",
    "headset": "audio-head",
    "earbuds": "audio-head",
    "earphones": "audio-head",
    "buds": "audio-head",
    "speaker": "audio-card",
    "speakers": "audio-card",
    "mouse": "input-mouse",
    "keyboard": "input-keyboard",
    "controller": "input-gaming",
    "gamepad": "input-gaming",
    "phone": "phone",
}


def match_devices(words: str, devices: list[BluetoothDevice]) -> list[BluetoothDevice]:
    """Paired devices the user's words name — by a word of the name ("sony") or by kind ("headphones")."""
    tokens = [token for token in normalize(words).split() if token not in _GENERIC]
    found = []
    for device in devices:
        name = normalize(device.name)
        by_name = any(
            t in name.split() or (len(t) >= 3 and t in name.replace(" ", ""))
            for t in tokens
            if t not in _DEVICE_KINDS
        )
        by_kind = any(device.icon.startswith(_DEVICE_KINDS[t]) for t in tokens if t in _DEVICE_KINDS)
        if by_name or by_kind:
            found.append(device)
    return found


class BluetoothDeviceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str = Field(
        min_length=1,
        max_length=80,
        description="the paired device as the user named it, e.g. 'headphones', 'sony'",
    )
    connect: bool = Field(default=True, description="true to connect, false to disconnect")


class BluetoothDeviceTool(Tool):
    name = "settings.bluetooth_device"
    description = (
        "Connect or disconnect a Bluetooth device by name or kind, e.g. headphones, speaker, mouse "
        "(SONU checks that it's paired)."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = BluetoothDeviceArgs
    grounded = ("device",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(BluetoothDeviceArgs, args)
        if problem := _missing(ctx, "bluetoothctl"):
            return problem
        verb = "Connect" if a.connect else "Disconnect"
        try:
            if choice is not None:
                device = await ctx.system.bluetooth_device(choice)
            else:
                if problem := _ungrounded(a.device, ctx, "the device"):
                    return problem
                matches = match_devices(a.device, await ctx.system.paired_devices())
                if not matches:
                    return ToolOutcome(
                        StepStatus.FAILED,
                        f"No paired Bluetooth device matches '{a.device}' — pair it once in "
                        "Bluetooth Manager, then SONU can connect it",
                    )
                if len(matches) > 1:
                    return ToolOutcome(
                        StepStatus.NEEDS_INPUT,
                        f"several paired devices match '{a.device}'",
                        question=f"Which device: {' or '.join(d.name for d in matches[:4])}?",
                        choices=Choices(
                            f"Which device should I {verb.lower()}?",
                            tuple(Choice(d.name, d.mac) for d in matches[:6]),
                        ),
                    )
                device = matches[0]
        except CommandError as exc:
            return _failed(exc)
        return Prepared(f"{verb} {device.name}", (device, a.connect))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        device, connect = cast(tuple[BluetoothDevice, bool], prepared.payload)
        word = "connected" if connect else "disconnected"
        try:
            if connect and not await ctx.system.bluetooth_powered():
                await ctx.system.set_bluetooth(True)
                await _settle(ctx.system.bluetooth_powered, bool, ctx.settings.verify.timeout_s)
            await ctx.system.connect_bluetooth(device.mac, connect)
            after = await _settle(
                lambda: ctx.system.bluetooth_device(device.mac),
                lambda d: d.connected is connect,
                CONNECT_VERIFY_S,
            )
        except CommandError as exc:
            return _failed(exc)
        check = Check(f"{device.name} is {word}", after.connected is connect, {"connected": after.connected})
        hint = " (is it switched on and in range?)" if connect else ""
        return outcome_from_checks(
            [check], f"{device.name} is {word}", f"{device.name} didn't get {word}{hint}"
        )


SETTINGS_TOOLS: tuple[Tool, ...] = (
    SetVolume(),
    SetBrightness(),
    SetBluetooth(),
    BluetoothDeviceTool(),
    SetWifi(),
    ConnectWifi(),
    MediaControl(),
    SetPowerMode(),
    SystemStatus(),
)
