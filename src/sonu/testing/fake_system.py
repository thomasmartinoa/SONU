"""A fake set of system programs (wpctl, brightnessctl, bluetoothctl, …) with state, for tests and evals.

It answers with the same output formats as the real programs and models the quirks SONU relies on:
the Bluetooth adapter boots rfkill-blocked (as on the owner's laptop) and can't power on until
unblocked, a laptop with an NVIDIA backlight next to the real one, "No players found", and a
power mode that may be unavailable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sonu.system.commands import CommandError, CommandResult, ProgramMissing

ALL_PROGRAMS = frozenset(
    {"wpctl", "brightnessctl", "bluetoothctl", "rfkill", "nmcli", "playerctl", "powerprofilesctl"}
)


@dataclass
class FakeSystem:
    installed: set[str] = field(default_factory=lambda: set(ALL_PROGRAMS))
    speaker: float = 0.5
    speaker_muted: bool = False
    mic: float = 0.8
    mic_muted: bool = False
    backlights: dict[str, tuple[int, int]] = field(
        default_factory=lambda: {"nvidia_0": (50, 100), "intel_backlight": (48000, 120000)}
    )
    bluetooth_adapter: bool = True
    bluetooth_service: bool = True  # False: bluetoothctl hangs until the runner's timeout
    bluetooth_blocked: bool = True  # how the owner's laptop boots
    bluetooth_powered: bool = False
    wifi: bool = True
    wifi_networks: list[str] = field(
        default_factory=lambda: ["Home: 5G", "hithere"]
    )  # saved in NetworkManager
    wifi_connection: str | None = "Home: 5G"
    # Paired Bluetooth devices: MAC → [name, icon, connected]
    bt_devices: dict[str, list[object]] = field(
        default_factory=lambda: {
            "AA:BB:CC:DD:EE:01": ["WH-1000XM4", "audio-headphones", False],
            "AA:BB:CC:DD:EE:02": ["MX Keys", "input-keyboard", False],
        }
    )
    player: str | None = "Playing"  # None: no player running
    tracks: list[str] = field(
        default_factory=lambda: ["Artist One Song", "Artist Two Song", "Artist Three Song"]
    )
    track: int = 0
    power_mode: str = "balanced"
    power_modes: tuple[str, ...] = ("performance", "balanced", "power-saver")
    calls: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def brightness(self) -> int:
        """The screen's brightness in percent (the non-NVIDIA backlight, as SONU picks it)."""
        device = next((d for d in self.backlights if "nvidia" not in d), next(iter(self.backlights)))
        current, maximum = self.backlights[device]
        return round(current * 100 / maximum)

    @property
    def bt_connected(self) -> list[str]:
        """Names of the connected Bluetooth devices."""
        return sorted(str(device[0]) for device in self.bt_devices.values() if device[2])

    def available(self, program: str) -> bool:
        return program in self.installed

    async def run(self, argv: Sequence[str], *, timeout_s: float = 5.0) -> CommandResult:
        args = tuple(argv)
        if args[0] not in self.installed:
            raise ProgramMissing(args[0])
        if args[0] == "bluetoothctl" and not self.bluetooth_service:
            raise CommandError(f"bluetoothctl didn't finish within {timeout_s:.0f}s")
        self.calls.append(args)
        handler = getattr(self, "_" + args[0].replace("-", "_"))
        code, out, err = handler(list(args[1:]))
        return CommandResult(args, code, out, err)

    # ------------------------------------------------------------------ programs
    def _wpctl(self, args: list[str]) -> tuple[int, str, str]:
        if args[0] == "get-volume":
            level, muted = (
                (self.speaker, self.speaker_muted) if "SINK" in args[1] else (self.mic, self.mic_muted)
            )
            return 0, f"Volume: {level:.2f}{' [MUTED]' if muted else ''}\n", ""
        if args[0] == "set-volume":
            node, value = args[-2], args[-1]
            level = min(float(value.rstrip("%")) / 100, 1.0)
            if "SINK" in node:
                self.speaker = level
            else:
                self.mic = level
            return 0, "", ""
        if args[0] == "set-mute":
            muted = args[2] == "1"
            if "SINK" in args[1]:
                self.speaker_muted = muted
            else:
                self.mic_muted = muted
            return 0, "", ""
        return 1, "", f"unknown wpctl command {args}\n"

    def _line(self, device: str) -> str:
        current, maximum = self.backlights[device]
        return f"{device},backlight,{current},{round(current * 100 / maximum)}%,{maximum}"

    def _brightnessctl(self, args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["-l", "-m"]:
            return 0, "".join(self._line(device) + "\n" for device in self.backlights), ""
        if "-d" in args:
            device = args[args.index("-d") + 1]
            if device not in self.backlights:
                return 1, "", f"Device '{device}' not found.\n"
            if "set" in args:
                _, maximum = self.backlights[device]
                percent = int(args[args.index("set") + 1].rstrip("%"))
                self.backlights[device] = (maximum * percent // 100, maximum)
                return 0, "", ""
            return 0, self._line(device) + "\n", ""
        return 1, "", "unsupported\n"

    def _rfkill(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["unblock", "bluetooth"]:
            self.bluetooth_blocked = False
        return 0, "", ""

    def _bluetoothctl(self, args: list[str]) -> tuple[int, str, str]:
        if not self.bluetooth_adapter:
            return 1, "No default controller available\n", ""
        if args == ["show"]:
            state = "on" if self.bluetooth_powered else ("off-blocked" if self.bluetooth_blocked else "off")
            return (
                0,
                (
                    "Controller 00:11:22:33:44:55 (public)\n\tName: archnitro\n"
                    f"\tPowered: {'yes' if self.bluetooth_powered else 'no'}\n\tPowerState: {state}\n"
                ),
                "",
            )
        if args[:1] == ["power"]:
            if args[1] == "on" and self.bluetooth_blocked:
                return 1, "Failed to set power on: org.bluez.Error.Failed\n", ""
            self.bluetooth_powered = args[1] == "on"
            if not self.bluetooth_powered:
                for device in self.bt_devices.values():
                    device[2] = False
            return 0, f"Changing power {args[1]} succeeded\n", ""
        if args == ["devices", "Paired"]:
            return 0, "".join(f"Device {mac} {d[0]}\n" for mac, d in self.bt_devices.items()), ""
        if args[:1] == ["info"] and args[1] in self.bt_devices:
            name, icon, connected = self.bt_devices[args[1]]
            return (
                0,
                (
                    f"Device {args[1]} (public)\n\tName: {name}\n\tAlias: {name}\n\tIcon: {icon}\n"
                    f"\tPaired: yes\n\tConnected: {'yes' if connected else 'no'}\n"
                ),
                "",
            )
        if args[:1] in (["connect"], ["disconnect"]) and args[1] in self.bt_devices:
            if not self.bluetooth_powered:
                return 1, "Failed to connect: org.bluez.Error.NotReady\n", ""
            self.bt_devices[args[1]][2] = args[0] == "connect"
            return 0, "Connection successful\n" if args[0] == "connect" else "Successful disconnected\n", ""
        return 1, "", "unsupported\n"

    @staticmethod
    def _terse(name: str) -> str:
        return name.replace("\\", "\\\\").replace(":", "\\:")

    def _nmcli(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["radio", "wifi"]:
            return 0, ("enabled" if self.wifi else "disabled") + "\n", ""
        if args[:2] == ["radio", "wifi"]:
            self.wifi = args[2] == "on"
            if not self.wifi:
                self.wifi_connection = None
            return 0, "", ""
        if args == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
            lines = [f"{self._terse(n)}:802-11-wireless" for n in self.wifi_networks]
            return 0, "\n".join([*lines, "Wired connection 1:802-3-ethernet"]) + "\n", ""
        if args == ["-t", "-f", "NAME,TYPE", "connection", "show", "--active"]:
            active = f"{self._terse(self.wifi_connection)}:802-11-wireless\n" if self.wifi_connection else ""
            return 0, active + "lo:loopback\n", ""
        if args[:5] == ["--wait", "20", "connection", "up", "id"]:
            name = args[5]
            if name not in self.wifi_networks:
                return 10, "", f"Error: unknown connection '{name}'.\n"
            if not self.wifi:
                return (
                    4,
                    "",
                    "Error: Connection activation failed: No suitable device found for this connection.\n",
                )
            self.wifi_connection = name
            return 0, "Connection successfully activated\n", ""
        return 1, "", "unsupported\n"

    def _playerctl(self, args: list[str]) -> tuple[int, str, str]:
        if self.player is None:
            return 1, "", "No players found\n"
        if args == ["status"]:
            return 0, self.player + "\n", ""
        if args[0] == "metadata":
            return 0, self.tracks[self.track] + "\n", ""
        transitions = {"play": "Playing", "pause": "Paused", "stop": "Stopped"}
        if args[0] in transitions:
            self.player = transitions[args[0]]
        elif args[0] == "play-pause":
            self.player = "Paused" if self.player == "Playing" else "Playing"
        elif args[0] == "next":
            self.track = min(self.track + 1, len(self.tracks) - 1)
        elif args[0] == "previous":
            self.track = max(self.track - 1, 0)
        return 0, "", ""

    def _powerprofilesctl(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["get"]:
            return 0, self.power_mode + "\n", ""
        if args[:1] == ["set"]:
            if args[1] not in self.power_modes:
                return 1, "", f"Failed to activate profile '{args[1]}'\n"
            self.power_mode = args[1]
            return 0, "", ""
        return 1, "", "unsupported\n"
