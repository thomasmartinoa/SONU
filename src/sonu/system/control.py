"""Reading and changing system settings with the programs the owner's rice already uses.

Every call builds its argv from fixed words and validated numbers. Reads parse the programs'
plain output (in the C locale); writes don't trust exit codes alone — tools read the setting back.
The exact behaviours copied from the rice (hypr-dotfiles) are noted where they matter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sonu.system.commands import CommandError, CommandResult, CommandRunner, ProgramMissing, SubprocessRunner

AudioTarget = Literal["speaker", "mic"]
PlayerAction = Literal["play", "pause", "toggle", "next", "previous", "stop"]
PowerMode = Literal["performance", "balanced", "power-saver"]

_AUDIO_NODES: dict[str, str] = {"speaker": "@DEFAULT_AUDIO_SINK@", "mic": "@DEFAULT_AUDIO_SOURCE@"}
_PLAYERCTL: dict[str, str] = {
    "play": "play",
    "pause": "pause",
    "toggle": "play-pause",
    "next": "next",
    "previous": "previous",
    "stop": "stop",
}
_VOLUME = re.compile(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)(\s*\[MUTED\])?")
_POWERED = re.compile(r"^\s*Powered:\s*(yes|no)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Volume:
    level: float  # 1.0 = 100%
    muted: bool

    @property
    def percent(self) -> int:
        return round(self.level * 100)


@dataclass(frozen=True, slots=True)
class Backlight:
    device: str
    current: int
    percent: int
    maximum: int


@dataclass(frozen=True, slots=True)
class Battery:
    name: str
    percent: int
    status: str  # Charging, Discharging, Full, Not charging, …


def parse_volume(text: str) -> Volume:
    """``wpctl get-volume`` → "Volume: 0.40" or "Volume: 0.40 [MUTED]"."""
    match = _VOLUME.search(text)
    if match is None:
        raise CommandError(f"Unexpected wpctl output: {text.strip()[:80]!r}")
    return Volume(float(match.group(1)), bool(match.group(2)))


def parse_backlight(line: str) -> Backlight | None:
    """``brightnessctl -m`` → "intel_backlight,backlight,48000,40%,120000"."""
    parts = line.strip().split(",")
    if len(parts) < 5:
        return None
    try:
        return Backlight(parts[0], int(parts[2]), int(parts[3].rstrip("%")), int(parts[4]))
    except ValueError:
        return None


def pick_backlight(listing: str) -> str | None:
    """The screen's backlight: the first one that isn't NVIDIA's (as the rice's brightness.sh does)."""
    devices = [backlight.device for line in listing.splitlines() if (backlight := parse_backlight(line))]
    preferred = [device for device in devices if "nvidia" not in device.lower()]
    candidates = preferred or devices
    return candidates[0] if candidates else None


def parse_bluetooth_powered(text: str) -> bool | None:
    """``bluetoothctl show`` → True/False from "Powered: yes/no"; None when there is no adapter."""
    match = _POWERED.search(text)
    return None if match is None else match.group(1) == "yes"


class SystemControl:
    def __init__(
        self, runner: CommandRunner | None = None, *, power_supply: Path = Path("/sys/class/power_supply")
    ) -> None:
        self.runner: CommandRunner = runner or SubprocessRunner()
        self.power_supply = power_supply

    def available(self, program: str) -> bool:
        return self.runner.available(program)

    async def _run(self, *argv: str) -> CommandResult:
        result = await self.runner.run(argv)
        if not result.ok:
            raise CommandError(result.problem())
        return result

    # ------------------------------------------------------------------ audio (wpctl)
    async def volume(self, target: AudioTarget) -> Volume:
        return parse_volume((await self._run("wpctl", "get-volume", _AUDIO_NODES[target])).stdout)

    async def set_volume(self, target: AudioTarget, level: float) -> None:
        # -l 1.0: never above 100%, like the rice's volume keys.
        percent = f"{round(max(0.0, min(level, 1.0)) * 100)}%"
        await self._run("wpctl", "set-volume", "-l", "1.0", _AUDIO_NODES[target], percent)

    async def set_mute(self, target: AudioTarget, muted: bool) -> None:
        await self._run("wpctl", "set-mute", _AUDIO_NODES[target], "1" if muted else "0")

    # ------------------------------------------------------------------ screen (brightnessctl)
    async def backlight(self) -> Backlight:
        listing = (await self._run("brightnessctl", "-l", "-m", "-c", "backlight")).stdout
        device = pick_backlight(listing)
        if device is None:
            raise CommandError("No screen backlight found")
        backlight = parse_backlight((await self._run("brightnessctl", "-m", "-d", device)).stdout)
        if backlight is None:
            raise CommandError(f"Unexpected brightnessctl output for {device}")
        return backlight

    async def set_brightness(self, device: str, percent: int) -> None:
        await self._run("brightnessctl", "-q", "-d", device, "set", f"{percent}%")

    # ------------------------------------------------------------------ Bluetooth (rfkill + bluetoothctl)
    async def bluetooth_powered(self) -> bool | None:
        try:
            result = await self.runner.run(("bluetoothctl", "show"))
        except ProgramMissing:
            raise
        except CommandError as exc:
            # Without bluetoothd, bluetoothctl waits forever ("Waiting to connect to bluetoothd…").
            raise CommandError(
                f"{exc} — is the Bluetooth service running? (systemctl start bluetooth)"
            ) from exc
        return parse_bluetooth_powered(result.stdout)

    async def set_bluetooth(self, on: bool) -> None:
        if on and self.available("rfkill"):
            # The adapter can't be powered on while rfkill soft-blocks it, which is how the owner's
            # laptop boots (hypr-dotfiles, Services/Bt.qml): unblock first.
            await self.runner.run(("rfkill", "unblock", "bluetooth"))
        # Exit codes vary between bluez versions; the tool reads "Powered:" back instead.
        await self.runner.run(("bluetoothctl", "power", "on" if on else "off"))

    # ------------------------------------------------------------------ Wi-Fi (nmcli)
    async def wifi_enabled(self) -> bool:
        return (await self._run("nmcli", "radio", "wifi")).stdout.strip() == "enabled"

    async def set_wifi(self, on: bool) -> None:
        await self._run("nmcli", "radio", "wifi", "on" if on else "off")

    # ------------------------------------------------------------------ media (playerctl)
    async def player_status(self) -> str | None:
        """Playing / Paused / Stopped, or None when no player is running."""
        result = await self.runner.run(("playerctl", "status"))
        status = result.stdout.strip()
        return status if result.ok and status else None

    async def player_track(self) -> str:
        result = await self.runner.run(("playerctl", "metadata", "--format", "{{artist}} {{title}}"))
        return result.stdout.strip() if result.ok else ""

    async def player(self, action: PlayerAction) -> None:
        await self._run("playerctl", _PLAYERCTL[action])

    # ------------------------------------------------------------------ power mode (powerprofilesctl)
    async def power_mode(self) -> str:
        return (await self._run("powerprofilesctl", "get")).stdout.strip()

    async def set_power_mode(self, mode: PowerMode) -> None:
        await self._run("powerprofilesctl", "set", mode)

    # ------------------------------------------------------------------ battery (sysfs, read-only)
    def batteries(self) -> list[Battery]:
        found = []
        for supply in sorted(self.power_supply.glob("BAT*")):
            try:
                percent = int((supply / "capacity").read_text().strip())
                status = (supply / "status").read_text().strip()
            except (OSError, ValueError):
                continue
            found.append(Battery(supply.name, percent, status))
        return found


__all__ = [
    "AudioTarget",
    "Backlight",
    "Battery",
    "CommandError",
    "PlayerAction",
    "PowerMode",
    "ProgramMissing",
    "SystemControl",
    "Volume",
    "parse_backlight",
    "parse_bluetooth_powered",
    "parse_volume",
    "pick_backlight",
]
