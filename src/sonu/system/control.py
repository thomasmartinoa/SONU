"""Reading and changing system settings with the programs the owner's rice already uses.

Every call builds its argv from fixed words and validated numbers. Reads parse the programs'
plain output (in the C locale); writes don't trust exit codes alone — tools read the setting back.
The exact behaviours copied from the rice (hypr-dotfiles) are noted where they matter.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
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
_DEVICE_LINE = re.compile(r"^Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) (.+)$", re.MULTILINE)
_MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
_WIFI_TYPE = "802-11-wireless"
CONNECT_TIMEOUT_S = 25.0
SEARCH_TIMEOUT_S = 30.0  # yt-dlp contacts YouTube
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_GEOMETRY = re.compile(r"^-?\d+,-?\d+ \d+x\d+$")
_SHELL_WORD = re.compile(r"^[a-z][a-z-]{0,30}$")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
# Only SONU's own timers (reminders.set names them) are ever listed or stopped.
REMINDER_UNIT = re.compile(r"^sonu-reminder-\d{8}-\d{6}-[0-9a-f]{4}$")
_REMINDER_PREFIX = "SONU reminder: "
_MIME = re.compile(r"^[\w.+-]+/[\w.+-]+$")
_DESKTOP_ID = re.compile(r"^[\w.+-]+\.desktop$")
_THEME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # hypr-theme's own rule: no "/" or leading "."
THEME_TIMEOUT_S = 60.0  # a theme switch re-renders every app's colours
MAX_CLIPBOARD_CHARS = 2000


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
class BluetoothDevice:
    mac: str
    name: str
    icon: str  # e.g. "audio-headphones", "input-keyboard" (bluez's device class)
    connected: bool


@dataclass(frozen=True, slots=True)
class Video:
    id: str
    title: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass(frozen=True, slots=True)
class AudioStream:
    """An app's sound output, as PipeWire sees it."""

    serial: int  # PipeWire's object.serial: never reused while PipeWire runs
    pid: int | None  # the process that plays it (application.process.id)
    running: bool  # sound is flowing now (not paused, corked or idle)


@dataclass(frozen=True, slots=True)
class Theme:
    id: str  # what hypr-theme takes, e.g. "tokyo-night"
    name: str  # what people call it, e.g. "Tokyo Night"
    mode: str  # "dark" or "light"
    current: bool = False


@dataclass(frozen=True, slots=True)
class Reminder:
    unit: str  # e.g. "sonu-reminder-20260927-124411-a068" (the .timer and .service share it)
    when: datetime | None  # local time it fires; None if systemd has no next time
    text: str


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


def parse_audio_streams(text: str) -> list[AudioStream]:
    """``pw-dump`` (JSON) → the audio output streams of apps."""
    try:
        objects = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError(f"Unexpected pw-dump output: {exc}") from None
    streams = []
    for obj in objects if isinstance(objects, list) else []:
        info = obj.get("info") if isinstance(obj, dict) else None
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict) or props.get("media.class") != "Stream/Output/Audio":
            continue
        serial, pid = props.get("object.serial"), props.get("application.process.id")
        if not isinstance(serial, int):
            continue
        pid = int(pid) if isinstance(pid, int | str) and str(pid).isdigit() else None
        streams.append(AudioStream(serial, pid, info.get("state") == "running"))
    return streams


def parse_bluetooth_powered(text: str) -> bool | None:
    """``bluetoothctl show`` → True/False from "Powered: yes/no"; None when there is no adapter."""
    match = _POWERED.search(text)
    return None if match is None else match.group(1) == "yes"


def split_terse(line: str) -> list[str]:
    """One line of ``nmcli -t`` output: fields split on ":", with "\\:" and "\\\\" unescaped."""
    fields, current, index = [], [], 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            current.append(line[index + 1])
            index += 2
            continue
        if char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    fields.append("".join(current))
    return fields


def parse_bluetooth_info(mac: str, text: str) -> BluetoothDevice:
    """``bluetoothctl info MAC`` → name, icon and whether it's connected."""

    def field(name: str) -> str:
        match = re.search(rf"^\s*{name}:\s*(.*)$", text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    return BluetoothDevice(
        mac, field("Name") or field("Alias") or mac, field("Icon"), field("Connected") == "yes"
    )


class SystemControl:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        power_supply: Path = Path("/sys/class/power_supply"),
        proc: Path = Path("/proc"),
        sys: Path = Path("/sys"),
        home: Path | None = None,
    ) -> None:
        self.runner: CommandRunner = runner or SubprocessRunner()
        self.power_supply = power_supply
        self.proc = proc
        self.sys = sys
        self.home = home or Path.home()

    def available(self, program: str) -> bool:
        return self.runner.available(program)

    async def _run(self, *argv: str, timeout_s: float = 5.0) -> CommandResult:
        result = await self.runner.run(argv, timeout_s=timeout_s)
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

    async def paired_devices(self) -> list[BluetoothDevice]:
        result = await self.runner.run(("bluetoothctl", "devices", "Paired"))
        if not result.ok or not result.stdout.strip():
            result = await self.runner.run(("bluetoothctl", "paired-devices"))  # bluez < 5.65
        devices = []
        for mac, _ in _DEVICE_LINE.findall(result.stdout):
            devices.append(await self.bluetooth_device(mac))
        return devices

    async def bluetooth_device(self, mac: str) -> BluetoothDevice:
        if not _MAC.match(mac):
            raise CommandError(f"Not a Bluetooth address: {mac!r}")
        return parse_bluetooth_info(mac, (await self.runner.run(("bluetoothctl", "info", mac))).stdout)

    async def connect_bluetooth(self, mac: str, connect: bool) -> None:
        if not _MAC.match(mac):
            raise CommandError(f"Not a Bluetooth address: {mac!r}")
        # Exit codes vary; the tool reads "Connected:" back instead.
        await self.runner.run(
            ("bluetoothctl", "connect" if connect else "disconnect", mac), timeout_s=CONNECT_TIMEOUT_S
        )

    # ------------------------------------------------------------------ Wi-Fi (nmcli)
    async def wifi_enabled(self) -> bool:
        return (await self._run("nmcli", "radio", "wifi")).stdout.strip() == "enabled"

    async def set_wifi(self, on: bool) -> None:
        await self._run("nmcli", "radio", "wifi", "on" if on else "off")

    async def wifi_saved(self) -> list[str]:
        """Names of the Wi-Fi networks NetworkManager has saved (it knows their passwords)."""
        result = await self._run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
        return [
            fields[0]
            for line in result.stdout.splitlines()
            if len(fields := split_terse(line)) >= 2 and fields[1] == _WIFI_TYPE
        ]

    async def wifi_active(self) -> str | None:
        result = await self._run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active")
        for line in result.stdout.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[1] == _WIFI_TYPE:
                return fields[0]
        return None

    async def connect_wifi(self, name: str) -> None:
        """Activate a saved network; the name is passed as one argv element (never through a shell)."""
        await self._run("nmcli", "--wait", "20", "connection", "up", "id", name, timeout_s=CONNECT_TIMEOUT_S)

    # ------------------------------------------------------------------ media (playerctl)
    async def player_status(self) -> str | None:
        """Playing / Paused / Stopped, or None when no player is running."""
        result = await self.runner.run(("playerctl", "status"))
        status = result.stdout.strip()
        return status if result.ok and status else None

    async def player_track(self) -> str:
        result = await self.runner.run(("playerctl", "metadata", "--format", "{{artist}} {{title}}"))
        return result.stdout.strip() if result.ok else ""

    async def now_playing(self) -> str:
        """'Title — Artist' of what the media controls show (read-only); '' if nothing."""
        result = await self.runner.run(("playerctl", "metadata", "--format", "{{title}}\t{{artist}}"))
        if not result.ok:
            return ""
        title, _, artist = result.stdout.strip().partition("\t")
        return f"{title} — {artist}" if title and artist else title or artist

    async def player(self, action: PlayerAction) -> None:
        await self._run("playerctl", _PLAYERCTL[action])

    async def players(self) -> list[str]:
        """Every media player the system knows (``playerctl -l``), e.g. a browser playing a video."""
        result = await self.runner.run(("playerctl", "-l"))
        return [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.ok else []

    async def player_state(self, name: str) -> tuple[str | None, str]:
        """(status, title) of one player; status None if it's gone."""
        status = await self.runner.run(("playerctl", "-p", name, "status"))
        title = await self.runner.run(("playerctl", "-p", name, "metadata", "--format", "{{title}}"))
        return (
            status.stdout.strip() or None
        ) if status.ok else None, title.stdout.strip() if title.ok else ""

    async def play_player(self, name: str) -> None:
        await self._run("playerctl", "-p", name, "play")

    async def audio_streams(self) -> list[AudioStream]:
        """Apps' sound outputs (``pw-dump``, read-only): lets SONU hear that a browser plays."""
        return parse_audio_streams((await self._run("pw-dump")).stdout)

    # ------------------------------------------------------------------ YouTube (yt-dlp, read-only lookup)
    async def find_video(self, query: str) -> Video | None:
        """The top YouTube result for ``query`` (the user's own words). Nothing is downloaded."""
        result = await self._run(
            "yt-dlp",
            "--ignore-config",
            "--no-warnings",
            "--flat-playlist",
            "--print",
            "%(id)s\t%(title)s",
            f"ytsearch1:{query}",
            timeout_s=SEARCH_TIMEOUT_S,
        )
        for line in result.stdout.splitlines():
            video_id, _, title = line.partition("\t")
            if _VIDEO_ID.match(video_id):
                return Video(video_id, title.strip() or video_id)
        return None

    # ------------------------------------------------------------------ reminders (systemd user timers)
    async def schedule_reminder(self, unit: str, when: datetime, text: str) -> None:
        """A transient user timer that shows a notification at ``when`` (local time)."""
        await self._run(
            "systemd-run",
            "--user",
            f"--unit={unit}",
            f"--description={_REMINDER_PREFIX}{text[:60]}",
            f"--on-calendar={when:%Y-%m-%d %H:%M:%S}",
            "--timer-property=AccuracySec=1s",
            "--",
            "notify-send",
            "--app-name=SONU",
            "--urgency=critical",
            "--",
            "SONU reminder",
            text,
        )

    async def reminders(self) -> list[Reminder]:
        """SONU's pending reminders, soonest first (``systemctl --user list-timers``, read-only)."""
        listing = await self._run(
            "systemctl", "--user", "list-timers", "--all", "--output=json", "sonu-reminder-*"
        )
        try:
            timers = json.loads(listing.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CommandError(f"Unexpected systemctl output: {exc}") from None
        found = []
        for timer in timers if isinstance(timers, list) else []:
            unit = str(timer.get("unit", "")).removesuffix(".timer") if isinstance(timer, dict) else ""
            if not REMINDER_UNIT.match(unit):
                continue
            micros = timer.get("next")
            when = (
                datetime.fromtimestamp(micros / 1_000_000) if isinstance(micros, int) and micros > 0 else None
            )
            description = await self._run(
                "systemctl", "--user", "show", f"{unit}.timer", "--property=Description", "--value"
            )
            text = description.stdout.strip().removeprefix(_REMINDER_PREFIX)
            found.append(Reminder(unit, when, text))
        return sorted(found, key=lambda r: (r.when is None, r.when or datetime.max))

    async def cancel_reminder(self, unit: str) -> None:
        if not REMINDER_UNIT.match(unit):
            raise CommandError(f"{unit!r} isn't one of SONU's reminders")
        await self._run("systemctl", "--user", "stop", f"{unit}.timer")

    async def timer_active(self, unit: str) -> bool:
        result = await self.runner.run(
            ("systemctl", "--user", "show", f"{unit}.timer", "--property=ActiveState", "--value")
        )
        return result.ok and result.stdout.strip() == "active"

    # ------------------------------------------------------------------ files (xdg-mime, read-only)
    async def mime_type(self, path: Path) -> str | None:
        """The file's type as the desktop sees it, e.g. "application/pdf"."""
        result = await self._run("xdg-mime", "query", "filetype", str(path))
        mime = result.stdout.strip().split(";")[0]
        return mime if _MIME.match(mime) else None

    async def default_app(self, mime: str) -> str | None:
        """The desktop-file id set to open ``mime`` files, e.g. "org.pwmt.zathura.desktop"."""
        if not _MIME.match(mime):
            return None
        result = await self._run("xdg-mime", "query", "default", mime)
        app = result.stdout.strip()
        return app if _DESKTOP_ID.match(app) else None

    # ------------------------------------------------------------------ screenshots (grim, slurp, wl-copy)
    def screenshot_dir(self) -> Path:
        """The rice's screenshot folder (~/Pictures/screenshot) if it exists, else ~/Pictures/Screenshots."""
        pictures = self.home / "Pictures"
        rice = pictures / "screenshot"
        return rice if rice.is_dir() else pictures / "Screenshots"

    async def select_area(self) -> str | None:
        """Let the user drag out an area (slurp); None if they cancelled."""
        result = await self.runner.run(("slurp",), timeout_s=120.0)
        geometry = result.stdout.strip()
        return geometry if result.ok and _GEOMETRY.match(geometry) else None

    async def screenshot(self, path: Path, geometry: str | None = None) -> None:
        if geometry is not None and not _GEOMETRY.match(geometry):
            raise CommandError(f"Not a screen area: {geometry!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        area = ("-g", geometry) if geometry else ()
        await self._run("grim", *area, str(path), timeout_s=15.0)

    async def copy_image(self, path: Path) -> None:
        # wl-copy keeps serving the clipboard from a forked helper: don't capture its output.
        image = await asyncio.to_thread(path.read_bytes)
        result = await self.runner.run(("wl-copy", "--type", "image/png"), input_bytes=image, capture=False)
        if not result.ok:
            raise CommandError(result.problem())

    async def clipboard_types(self) -> list[str]:
        result = await self.runner.run(("wl-paste", "--list-types"))
        return result.stdout.split() if result.ok else []

    # ------------------------------------------------------------ clipboard (private: never sent anywhere)
    async def clipboard_text(self) -> str | None:
        """The copied text (at most MAX_CLIPBOARD_CHARS), or None if what's copied isn't text."""
        types = await self.clipboard_types()
        if not any(t.startswith("text/") or t in ("UTF8_STRING", "STRING", "TEXT") for t in types):
            return None
        result = await self._run("wl-paste", "--no-newline", "--type", "text/plain")
        return result.stdout[:MAX_CLIPBOARD_CHARS]

    async def clear_clipboard(self) -> None:
        await self._run("wl-copy", "--clear")

    # ------------------------------------------------------------------ the rice's theme and wallpaper
    async def themes(self) -> list[Theme]:
        """The rice's themes (``hypr-theme json``, what its own picker reads)."""
        try:
            items = json.loads((await self._run("hypr-theme", "json")).stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CommandError(f"Unexpected hypr-theme output: {exc}") from None
        return [
            Theme(
                str(item["id"]),
                str(item.get("name") or item["id"]),
                str(item.get("mode", "")),
                bool(item.get("current")),
            )
            for item in items
            if isinstance(items, list)
            if isinstance(item, dict) and _THEME_NAME.match(str(item.get("id", "")))
        ]

    async def current_theme(self) -> str:
        return (await self._run("hypr-theme", "current")).stdout.strip()

    async def switch_theme(self, how: Literal["set", "next", "toggle"], name: str | None = None) -> None:
        """``hypr-theme set <id>`` / ``next`` / ``toggle`` (toggle prefers the -light/-dark sibling)."""
        if how == "set":
            if name is None or not _THEME_NAME.match(name):
                raise CommandError(f"Not a theme name: {name!r}")
            await self._run("hypr-theme", "set", name, timeout_s=THEME_TIMEOUT_S)
        else:
            await self._run("hypr-theme", how, timeout_s=THEME_TIMEOUT_S)

    def theme_backgrounds(self) -> list[Path]:
        """The current theme's wallpapers (what ``hypr-wall next`` cycles through)."""
        folder = self.home / ".config" / "hypr-theme" / "current" / "theme" / "backgrounds"
        try:
            return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file())
        except OSError:
            return []

    async def wallpaper(self) -> Path | None:
        current = (await self._run("hypr-wall", "current")).stdout.strip()
        return Path(current) if current else None

    async def change_wallpaper(self, image: Path | None = None) -> None:
        """``hypr-wall set <image>`` (an existing picture SONU found), or ``next`` in the theme."""
        if image is None:
            await self._run("hypr-wall", "next", timeout_s=THEME_TIMEOUT_S)
        elif image.suffix.lower() in IMAGE_SUFFIXES and await asyncio.to_thread(image.is_file):
            await self._run("hypr-wall", "set", str(image), timeout_s=THEME_TIMEOUT_S)
        else:
            raise CommandError(f"Not a picture: {image}")

    async def show_brightness_osd(self, percent: int) -> None:
        """The rice's on-screen popup, as its brightness keys show it (brightness.sh)."""
        await self._run("qs", "ipc", "call", "osd", "brightness", str(max(0, min(100, percent))))

    # ------------------------------------------------------------------ the rice's switches (Quickshell IPC)
    async def shell_call(self, target: str, function: str) -> str:
        """``qs ipc call target function`` → its return value (the rice's own switches)."""
        if not (_SHELL_WORD.match(target) and _SHELL_WORD.match(function)):
            raise CommandError(f"Not a shell switch: {target}.{function}")
        return (await self._run("qs", "ipc", "call", target, function)).stdout.strip()

    # ------------------------------------------------------------------ laptop health (read-only)
    def _cpu_times(self) -> tuple[int, int]:
        fields = (self.proc / "stat").read_text().splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        return idle, sum(values)

    async def cpu_percent(self) -> float | None:
        try:
            idle1, total1 = self._cpu_times()
            await asyncio.sleep(0.3)
            idle2, total2 = self._cpu_times()
        except (OSError, ValueError, IndexError):
            return None
        if total2 == total1:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (idle2 - idle1) / (total2 - total1))))

    def memory(self) -> tuple[int, int] | None:
        """(used, total) in bytes, from /proc/meminfo (used = total - available)."""
        try:
            info = {
                line.split(":")[0]: int(line.split()[1]) * 1024
                for line in (self.proc / "meminfo").read_text().splitlines()
                if len(line.split()) >= 2
            }
            return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]
        except (OSError, ValueError, KeyError):
            return None

    def disk(self) -> tuple[int, int] | None:
        """(free, total) bytes of the partition holding the home folder."""
        try:
            usage = shutil.disk_usage(self.home)
        except OSError:
            return None
        return usage.free, usage.total

    def cpu_temperature(self) -> float | None:
        """°C of the CPU: hwmon coretemp/k10temp, else the x86_pkg_temp zone, else the hottest zone."""
        for hwmon in sorted((self.sys / "class" / "hwmon").glob("hwmon*")):
            try:
                if (hwmon / "name").read_text().strip() in ("coretemp", "k10temp", "zenpower"):
                    return int((hwmon / "temp1_input").read_text()) / 1000
            except (OSError, ValueError):
                continue
        zones: list[tuple[str, float]] = []
        for zone in sorted((self.sys / "class" / "thermal").glob("thermal_zone*")):
            try:
                zones.append(((zone / "type").read_text().strip(), int((zone / "temp").read_text()) / 1000))
            except (OSError, ValueError):
                continue
        package = [temp for kind, temp in zones if kind == "x86_pkg_temp"]
        if package:
            return package[0]
        return max((temp for _, temp in zones), default=None)

    def uptime_s(self) -> float | None:
        try:
            return float((self.proc / "uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    async def gpu(self) -> tuple[int, int, int, int] | None:
        """(utilisation %, °C, MiB used, MiB total) from nvidia-smi."""
        result = await self.runner.run(
            (
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            )
        )
        try:
            util, temp, used, total = (int(v.strip()) for v in result.stdout.splitlines()[0].split(","))
        except (ValueError, IndexError):
            return None
        return util, temp, used, total

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
    "BluetoothDevice",
    "CommandError",
    "PlayerAction",
    "PowerMode",
    "ProgramMissing",
    "SystemControl",
    "Video",
    "Volume",
    "parse_backlight",
    "parse_bluetooth_powered",
    "parse_volume",
    "pick_backlight",
]
