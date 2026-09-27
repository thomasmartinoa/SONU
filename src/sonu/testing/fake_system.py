"""A fake set of system programs (wpctl, brightnessctl, bluetoothctl, …) with state, for tests and evals.

It answers with the same output formats as the real programs and models the quirks SONU relies on:
the Bluetooth adapter boots rfkill-blocked (as on the owner's laptop) and can't power on until
unblocked, a laptop with an NVIDIA backlight next to the real one, "No players found", and a
power mode that may be unavailable.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sonu.system.commands import CommandError, CommandResult, ProgramMissing
from sonu.testing.fake_hyprland import FAKE_PID_BASE, app_pid

ALL_PROGRAMS = frozenset(
    {
        "wpctl",
        "pw-dump",
        "xdg-mime",
        "hypr-theme",
        "hypr-wall",
        "brightnessctl",
        "bluetoothctl",
        "rfkill",
        "nmcli",
        "playerctl",
        "powerprofilesctl",
        "yt-dlp",
        "systemd-run",
        "systemctl",
        "notify-send",
        "grim",
        "slurp",
        "wl-copy",
        "wl-paste",
        "qs",
        "nvidia-smi",
    }
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


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
    # YouTube: what yt-dlp finds (query → (id, title)); unknown queries get a made-up video.
    videos: dict[str, tuple[str, str]] = field(default_factory=dict)
    last_video: tuple[str, str] | None = None
    # Other media players, e.g. a browser tab playing a video: name → [title, status].
    other_players: dict[str, list[str]] = field(default_factory=dict)
    browser_autoplay: bool = False  # False: the browser holds a new video back until "play"
    browser_mpris: bool = True  # False: the browser never shows up in the media controls
    # Apps' sound outputs (pw-dump): [serial, pid, running]. A page the browser autoplays adds one.
    streams: list[list[int | bool]] = field(default_factory=list)
    timers: dict[str, str] = field(default_factory=dict)  # systemd unit → notification text
    timer_times: dict[str, datetime] = field(default_factory=dict)  # systemd unit → when it fires
    power_requests: list[str] = field(default_factory=list)  # systemctl suspend / reboot / poweroff
    inhibited_by: str | None = None  # a program holding a sleep/shutdown lock
    area: str | None = "10,20 300x200"  # what slurp returns (None: the user cancelled)
    clipboard_type: str | None = None  # None with clipboard_text: text is copied
    clipboard_text: str | None = None
    theme_names: list[str] = field(
        default_factory=lambda: ["catppuccin-mocha", "hyprmono", "hyprmono-light", "tokyo-night"]
    )
    theme: str = "hyprmono"
    backgrounds: list[str] = field(default_factory=list)  # the theme's wallpapers (full paths)
    wallpaper: str | None = None
    osd: list[str] = field(default_factory=list)  # popups the rice's OSD showed ("brightness" value)
    shell: dict[str, str] = field(
        default_factory=lambda: {"nightlight": "off", "caffeine": "off", "dnd": "false", "lock": "unlocked"}
    )
    # Switches whose status lags behind once, like the rice's night light (its status still says the
    # old state after on/off until it's asked again).
    stale_once: set[str] = field(default_factory=set)
    notifications: int = 3
    # xdg-mime: file suffix → type, type → the desktop-file id set to open it.
    mime_types: dict[str, str] = field(
        default_factory=lambda: {
            ".png": "image/png",
            ".pdf": "application/pdf",
            ".zip": "application/zip",
            ".sh": "application/x-shellscript",
            ".desktop": "application/x-desktop",
        }
    )
    default_apps: dict[str, str] = field(
        default_factory=lambda: {"image/png": "imv.desktop", "application/zip": "thunar.desktop"}
    )
    gpu_line: str = "12, 55, 4143, 8192"
    last_input: bytes | None = None
    shots: list[str] = field(default_factory=list)  # screenshot files written by grim

    @property
    def night_light(self) -> str:
        return self.shell["nightlight"]

    @property
    def keep_awake(self) -> str:
        return self.shell["caffeine"]

    @property
    def do_not_disturb(self) -> bool:
        return self.shell["dnd"] == "true"

    @property
    def screenshots(self) -> int:
        return len(self.shots)

    @property
    def videos_playing(self) -> int:
        players = sum(1 for _, status in self.other_players.values() if status == "Playing")
        if self.browser_mpris:
            return players
        # Without media controls, a browser's video shows only as sound.
        return players + sum(1 for _, pid, running in self.streams if running and int(pid) >= FAKE_PID_BASE)

    @property
    def reminders(self) -> int:
        return len(self.timers)

    def page_opened(self, url: str, window_class: str = "zen") -> None:
        """The fake browser opened ``url``: a YouTube watch page registers a media player (MPRIS),
        and makes sound if the browser lets it autoplay."""
        if "youtube.com/watch" not in url or not self.last_video:
            return
        if self.browser_mpris:
            self.other_players["firefox.instance_1_42"] = [
                self.last_video[1],
                "Playing" if self.browser_autoplay else "Paused",
            ]
        if self.browser_autoplay:
            self.streams.append([100 + len(self.streams), app_pid(window_class), True])

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

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 5.0,
        input_bytes: bytes | None = None,
        capture: bool = True,
    ) -> CommandResult:
        args = tuple(argv)
        self.last_input = input_bytes
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
        if args == ["-l"]:
            names = (["applemusic"] if self.player is not None else []) + list(self.other_players)
            return (0, "".join(f"{n}\n" for n in names), "") if names else (1, "", "No players found\n")
        if args[:1] == ["-p"]:
            name, rest = args[1], args[2:]
            if name != "applemusic":
                return self._other_player(name, rest)
            args = rest
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

    def _other_player(self, name: str, args: list[str]) -> tuple[int, str, str]:
        if name not in self.other_players:
            return 1, "", "No players found\n"
        player = self.other_players[name]
        if args == ["status"]:
            return 0, player[1] + "\n", ""
        if args[:1] == ["metadata"]:
            return 0, player[0] + "\n", ""
        if args == ["play"]:
            player[1] = "Playing"
        return 0, "", ""

    def _xdg_mime(self, args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["query", "filetype"]:
            suffix = Path(args[2]).suffix.lower()
            return 0, self.mime_types.get(suffix, "application/octet-stream") + "\n", ""
        if args[:2] == ["query", "default"]:
            return 0, self.default_apps.get(args[2], "") + "\n", ""
        return 1, "", "usage\n"

    def _pw_dump(self, args: list[str]) -> tuple[int, str, str]:
        nodes = [
            {
                "id": 50 + int(serial),
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running" if running else "idle",
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "object.serial": serial,
                        "application.process.id": pid,
                    },
                },
            }
            for serial, pid, running in self.streams
        ]
        return 0, json.dumps([{"id": 0, "type": "PipeWire:Interface:Core", "info": {}}, *nodes]), ""

    def _yt_dlp(self, args: list[str]) -> tuple[int, str, str]:
        query = args[-1].removeprefix("ytsearch1:")
        made_up = (
            "vid" + str(zlib.crc32(query.encode()) % 10**8).zfill(8),
            f"{query.title()} (Official Video)",
        )
        video = self.videos.get(query, made_up)
        self.last_video = video
        return 0, f"{video[0]}\t{video[1]}\n", ""

    def _systemd_run(self, args: list[str]) -> tuple[int, str, str]:
        unit = next(a.removeprefix("--unit=") for a in args if a.startswith("--unit="))
        self.timers[unit] = args[-1]
        calendar = next(a.removeprefix("--on-calendar=") for a in args if a.startswith("--on-calendar="))
        self.timer_times[unit] = datetime.strptime(calendar, "%Y-%m-%d %H:%M:%S")
        return 0, f"Running timer as unit: {unit}.timer\n", ""

    def _systemctl(self, args: list[str]) -> tuple[int, str, str]:
        if args[0] in ("suspend", "reboot", "poweroff"):
            self.power_requests.append(args[0])
            return (
                (1, "", f'Operation inhibited by "{self.inhibited_by}"\n')
                if self.inhibited_by
                else (0, "", "")
            )
        if args[1] == "list-timers":
            timers = [
                {"next": int(self.timer_times[unit].timestamp() * 1_000_000), "unit": f"{unit}.timer"}
                for unit in self.timers
            ]
            return 0, json.dumps(timers), ""
        unit = args[2].removesuffix(".timer")
        if args[1] == "stop":
            self.timers.pop(unit, None)
            return 0, "", ""
        if args[3] == "--property=Description":
            return 0, f"SONU reminder: {self.timers.get(unit, '')}\n", ""
        return 0, ("active" if unit in self.timers else "inactive") + "\n", ""

    def _notify_send(self, args: list[str]) -> tuple[int, str, str]:
        return 0, "", ""

    def _grim(self, args: list[str]) -> tuple[int, str, str]:
        from pathlib import Path

        Path(args[-1]).write_bytes(PNG)
        self.shots.append(args[-1])
        return 0, "", ""

    def _slurp(self, args: list[str]) -> tuple[int, str, str]:
        return (0, self.area + "\n", "") if self.area else (1, "", "selection cancelled\n")

    def _wl_copy(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["--clear"]:
            self.clipboard_type, self.clipboard_text = None, None
            return 0, "", ""
        if self.last_input and self.last_input.startswith(PNG[:8]):
            self.clipboard_type = "image/png"
        return 0, "", ""

    def _wl_paste(self, args: list[str]) -> tuple[int, str, str]:
        if self.clipboard_type is None and self.clipboard_text is None:
            return 1, "", "Nothing is copied\n"
        if args == ["--list-types"]:
            types = (
                [self.clipboard_type] if self.clipboard_type else ["text/plain;charset=utf-8", "text/plain"]
            )
            return 0, "".join(f"{t}\n" for t in types), ""
        return 0, self.clipboard_text or "", ""

    def _hypr_theme(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["json"]:
            themes = [
                {
                    "id": n,
                    "name": n.replace("-", " ").title(),
                    "mode": "light" if "light" in n else "dark",
                    "current": n == self.theme,
                }
                for n in self.theme_names
            ]
            return 0, json.dumps(themes), ""
        if args == ["current"]:
            return 0, self.theme + "\n", ""
        if args[0] == "set":
            if args[1] not in self.theme_names:
                return 1, "", f"hypr-theme: no such theme: {args[1]}\n"
            self.theme = args[1]
        elif args[0] == "next":
            index = self.theme_names.index(self.theme)
            self.theme = self.theme_names[(index + 1) % len(self.theme_names)]
        elif args[0] == "toggle":  # hypr-theme prefers the sibling that differs by -light
            sibling = (
                self.theme.removesuffix("-light") if self.theme.endswith("-light") else f"{self.theme}-light"
            )
            self.theme = sibling if sibling in self.theme_names else self.theme_names[0]
        return 0, "", ""

    def _hypr_wall(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["current"]:
            return 0, (self.wallpaper or "") + "\n", ""
        if args[0] == "set":
            self.wallpaper = args[1]
        elif args[0] == "next" and self.backgrounds:
            index = self.backgrounds.index(self.wallpaper) if self.wallpaper in self.backgrounds else -1
            self.wallpaper = self.backgrounds[(index + 1) % len(self.backgrounds)]
        return 0, "", ""

    def _qs(self, args: list[str]) -> tuple[int, str, str]:
        target, function = args[2], args[3]
        if target in ("nightlight", "caffeine"):
            if function in ("on", "off"):
                if target in self.stale_once and self.shell[target] != function:
                    self.stale_once.discard(target)
                    return 0, "", ""
                self.shell[target] = function
                return 0, "", ""
            return 0, self.shell[target] + "\n", ""
        if target == "notifications":
            if function == "dnd":
                self.shell["dnd"] = "false" if self.shell["dnd"] == "true" else "true"
                return 0, self.shell["dnd"] + "\n", ""
            if function == "clear":
                self.notifications = 0
                return 0, "", ""
            return 0, f"{self.notifications}\n", ""
        if target == "osd":
            self.osd.append(args[4] if len(args) > 4 else function)
            return 0, "", ""
        if target == "lock":
            if function == "lock":
                self.shell["lock"] = "locked"
                return 0, "", ""
            return 0, self.shell["lock"] + "\n", ""
        return 1, "", f"No target {target} in shell\n"

    def _nvidia_smi(self, args: list[str]) -> tuple[int, str, str]:
        return 0, self.gpu_line + "\n", ""

    def _powerprofilesctl(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["get"]:
            return 0, self.power_mode + "\n", ""
        if args[:1] == ["set"]:
            if args[1] not in self.power_modes:
                return 1, "", f"Failed to activate profile '{args[1]}'\n"
            self.power_mode = args[1]
            return 0, "", ""
        return 1, "", "unsupported\n"
