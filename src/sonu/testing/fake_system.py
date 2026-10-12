"""A fake set of system programs (wpctl, brightnessctl, bluetoothctl, …) with state, for tests and evals.

It answers with the same output formats as the real programs and models the quirks SONU relies on:
the Bluetooth adapter boots rfkill-blocked (as on the owner's laptop) and can't power on until
unblocked, a laptop with an NVIDIA backlight next to the real one, "No players found", and a
power mode that may be unavailable.
"""

from __future__ import annotations

import json
import shutil
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from sonu.system.commands import CommandError, CommandResult, ProgramMissing
from sonu.testing.fake_github import FakeGitHub
from sonu.testing.fake_hyprland import FAKE_PID_BASE, app_pid
from sonu.testing.fake_kitty import FakeKitty

ALL_PROGRAMS = frozenset(
    {
        "wpctl",
        "pw-dump",
        "xdg-mime",
        "orrery-theme",
        "orrery-wall",
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
        "kitty",
        "gh",
        "pacman",
        "yay",
        "pkexec",
        "python3",
        "wlrctl",
        "wtype",
        "git",
        "gio",
        "mpv",
        "coredumpctl",
        "journalctl",
        "pdftotext",
        "soffice",
    }
)
# A repository for `git` in the fake home's projects: on a branch with one commit not pushed, two files
# changed (one staged as new) and one untracked.
GIT_REPO: dict[str, str] = {
    "status": "## main...origin/main [ahead 1]\0 M src/app.py\0A  src/login.py\0?? notes.txt\0",
    "numstat": "12\t3\tsrc/app.py\n40\t0\tsrc/login.py\n",
    "log": "abc1234\x1f2 hours ago\x1fAdd the login page\x1e\ndef5678\x1f1 day ago\x1fFirst commit\x1e\n",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@dataclass
class FakeSystem:
    installed: set[str] = field(default_factory=lambda: set(ALL_PROGRAMS))
    trash_dir: Path | None = None  # gio trash's trash (D75); None: gio trash fails
    # What went wrong (D81): failed services, crashes (time, program, signal), errors (program, message).
    failed_units: list[str] = field(default_factory=list)
    failed_user_units: list[str] = field(default_factory=list)
    crashed: list[tuple[float, str, int]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    user_errors: list[tuple[str, str]] = field(default_factory=list)
    gpu_apps: list[tuple[str, int]] = field(default_factory=lambda: [("/usr/bin/ollama", 3600)])
    signals: list[tuple[int, int]] = field(default_factory=list)  # (pid, signal) asked to quit
    office_profiles: list[str] = field(default_factory=list)  # LibreOffice profiles SONU used (D82)
    proc: Path | None = None  # the fake /proc a quit process disappears from
    stubborn: set[int] = field(default_factory=set)
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
    spotify_results: dict[str, tuple[str, str]] = field(default_factory=dict)  # search → (title, artist)
    song_details: dict[str, tuple[str, str]] = field(default_factory=dict)  # player → (artist, track id)
    # Apps' sound outputs (pw-dump): [serial, pid, running]. A page the browser autoplays adds one.
    streams: list[list[int | bool]] = field(default_factory=list)
    mic_users: list[str] = field(default_factory=list)  # apps recording the microphone (a call)
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
    kitty: FakeKitty = field(default_factory=FakeKitty)  # SONU's terminals (kitty @ remote control)
    github: FakeGitHub = field(default_factory=FakeGitHub)  # gh: pull requests
    # Packages: official repos (name → repo, version, description, size), the AUR (name → version,
    # description, votes), what's installed, and how the password prompt is answered.
    repo_packages: dict[str, tuple[str, str, str, str]] = field(
        default_factory=lambda: {
            "vlc": ("extra", "3.0.21-1", "Multi-platform MPEG, VCD/DVD, and DivX player", "2.10 MiB"),
            "obs-studio": (
                "extra",
                "32.2.2-1",
                "Free, open source software for live streaming and recording",
                "6.69 MiB",
            ),
            "spotify-launcher": (
                "extra",
                "0.6.6-2",
                "Client for spotify's apt repository in Rust for Arch Linux",
                "1.20 MiB",
            ),
            "spotify-player": ("extra", "0.25.1-1", "A command driven spotify player", "4.00 MiB"),
            "spotifyd": (
                "extra",
                "0.4.2-1",
                "Lightweight spotify streaming daemon with spotify connect support",
                "3.00 MiB",
            ),
        }
    )
    aur_packages: dict[str, tuple[str, str, int]] = field(
        default_factory=lambda: {
            "spotify": ("1:1.2.96.518-2", "A proprietary music streaming service", 276),
            "visual-studio-code-bin": ("1.106.0-1", "Visual Studio Code (vscode)", 1710),
        }
    )
    installed_packages: dict[str, str] = field(default_factory=lambda: {"kitty": "0.49.1-1"})
    # Removing (pacman -Rs): what each installed package alone pulled in, and what needs it.
    only_needs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # git: folder name → its repository (GIT_REPO's keys); other folders aren't repositories.
    git_repos: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"SONU": GIT_REPO, "shop-app": GIT_REPO}
    )
    # A project's command run in SONU's window (run_task.py): the command → (exit code, output);
    # exit code None: still running. Not listed: exit 0, "3 passed".
    task_results: dict[str, tuple[int | None, str]] = field(default_factory=dict)
    task_notices: list[str] = field(default_factory=list)  # --notify labels of the runs started
    tasks_run: list[str] = field(default_factory=list)
    polkit_answer: str = "allow"  # "dismiss": the user cancels the system's password prompt
    root_secret: str = "Hunter2!"  # noqa: S105 — the (fake) password polkit's text agent accepts
    # Accessibility (AT-SPI through SONU's helper): pid → the app's controls, as the helper reports
    # them; "then" (optional) is the control's name after a press (Play → Pause).
    a11y_apps: dict[int, list[dict[str, object]]] = field(default_factory=dict)
    # The pointer and keyboard (M6 part 3): what was clicked/typed; a click that changes the screen?
    clicks: list[str] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)
    click_changes_screen: bool = True
    screen_changes: int = 0
    presses: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.kitty.on_run = self.run_in_terminal
        self.kitty.on_password = self.password_typed

    def password_typed(self, argv: list[str], typed: str) -> str:
        if typed != self.root_secret:
            return (
                "polkit-agent-helper-1: pam_authenticate failed: Authentication failure\n"
                "==== AUTHENTICATION FAILED ====\n[sonu] finished (exit 127)"
            )
        return self.run_in_terminal(argv) + "[sonu] finished (exit 0)\n"

    def run_in_terminal(self, argv: list[str]) -> str:
        """A program started in a SONU terminal (an install, a project's command): its output."""
        if len(argv) > 2 and argv[1].endswith("run_task.py"):
            rest = argv[3:]
            if rest and rest[0].startswith("--notify="):  # D83: who to tell when a long run ends
                self.task_notices.append(rest[0].removeprefix("--notify="))
                rest = rest[1:]
            status, command = Path(argv[2]), " ".join(rest)
            self.tasks_run.append(command)
            code, output = self.task_results.get(command, (0, "===== 3 passed in 0.12s ====="))
            if code is None:
                return f"$ {command}\n\n{output}"
            status.write_text(str(code))
            return f"$ {command}\n\n{output}\n\n[sonu] finished (exit {code})\n"
        name = argv[-1]
        if argv[:3] == ["pkexec", "pacman", "-Rs"]:
            if self.polkit_answer == "dismiss":
                return "Error executing command as another user: Request dismissed\n"
            if name not in self.installed_packages:
                return f"error: target not found: {name}\n"
            for gone in (name, *self.only_needs.get(name, ())):
                self.installed_packages.pop(gone, None)
            return f"checking dependencies...\nremoving {name}...\n"
        if argv[:2] == ["pkexec", "pacman"] or argv[0] == "yay":
            if self.polkit_answer == "dismiss":
                return "Error executing command as another user: Request dismissed\n"
            known = self.repo_packages.get(name) if argv[0] == "pkexec" else None
            aur = self.aur_packages.get(name) if argv[0] == "yay" else None
            if known is None and aur is None:
                return f"error: target not found: {name}\n"
            self.installed_packages[name] = known[1] if known else aur[0] if aur else ""
            return f"resolving dependencies...\ninstalling {name}...\n"
        return ""

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

    def kill(self, pid: int, sig: int) -> None:
        """A process asked to quit (D81) — recorded, never sent: the fake /proc's pids are made up. A
        program in ``stubborn`` ignores it; the others are gone from ``proc`` (when it's set)."""
        self.signals.append((pid, sig))
        if self.proc is not None and pid not in self.stubborn:
            shutil.rmtree(self.proc / str(pid), ignore_errors=True)

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
        program = Path(args[0]).name  # "/usr/bin/python3" → python3
        if program not in self.installed:
            raise ProgramMissing(args[0])
        if args[0] == "bluetoothctl" and not self.bluetooth_service:
            raise CommandError(f"bluetoothctl didn't finish within {timeout_s:.0f}s")
        self.calls.append(args)
        handler = getattr(self, "_" + program.replace("-", "_"))
        code, out, err = handler(list(args[1:]))
        return CommandResult(args, code, out, err)

    # ------------------------------------------------------------------ programs
    def _python3(self, args: list[str]) -> tuple[int, str, str]:
        """SONU's accessibility helper (a11y_helper.py) on the fake apps."""
        command, rest = args[1], args[2:]
        controls = self.a11y_apps.get(int(rest[0])) if rest else None
        if controls is None:
            return 0, json.dumps({"error": "not-exposed"}), ""
        if command == "controls":
            shown = [{k: v for k, v in c.items() if k != "then"} for c in controls]
            return 0, json.dumps({"app": "fake", "controls": shown}), ""
        path, name, action = rest[1], rest[2], rest[3]
        control = next((c for c in controls if c["path"] == path), None)
        if control is None or control["name"] != name:
            return 0, json.dumps({"error": "moved"}), ""
        before = {"name": name, "pressed": bool(control.get("pressed"))}
        self.presses.append(f"{name}:{action}")
        control["name"] = control.get("then", name)
        return (
            0,
            json.dumps(
                {"pressed": True, "before": before, "after": {"name": control["name"], "pressed": False}}
            ),
            "",
        )

    def _pacman(self, args: list[str]) -> tuple[int, str, str]:
        if args[0] == "-Q":
            version = self.installed_packages.get(args[1])
            return (
                (0, f"{args[1]} {version}\n", "")
                if version
                else (1, "", f"error: package '{args[1]}' was not found\n")
            )
        if args[0] == "-Si":
            found = self.repo_packages.get(args[1])
            if found is None:
                return 1, "", f"error: package '{args[1]}' was not found\n"
            repo, version, description, size = found
            return (
                0,
                (
                    f"Repository      : {repo}\nName            : {args[1]}\nVersion         : {version}\n"
                    f"Description     : {description}\nDownload Size   : {size}\n"
                ),
                "",
            )
        if args[0] == "-Qs":
            terms = [t.lower() for t in args[1:] if t != "--"]
            listed = [
                f"local/{name} {version}\n    {self.repo_packages.get(name, ('', '', name, ''))[2]}\n"
                for name, version in sorted(self.installed_packages.items())
                if all(
                    t in f"{name} {self.repo_packages.get(name, ('', '', '', ''))[2]}".lower() for t in terms
                )
            ]
            return (0, "".join(listed), "") if listed else (1, "", "")
        if args[0] == "-Rsp":
            name = args[-1]
            if name not in self.installed_packages:
                return 1, "", f"error: target not found: {name}\n"
            if needs := self.required_by.get(name):
                broken = "".join(
                    f":: removing {name} breaks dependency '{name}' required by {n}\n" for n in needs
                )
                return 1, broken, "error: failed to prepare transaction (could not satisfy dependencies)\n"
            names = (name, *self.only_needs.get(name, ()))
            return 0, "".join(f"{n} {self.installed_packages.get(n, '1.0-1')}\n" for n in names), ""
        if args[0] == "-Ss":
            terms = [t.lower() for t in args[1:]]
            lines = [
                f"{repo}/{name} {version}\n    {description}\n"
                for name, (repo, version, description, _) in sorted(self.repo_packages.items())
                if all(t in f"{name} {description}".lower() for t in terms)
            ]
            return (0, "".join(lines), "") if lines else (1, "", "")
        return 1, "", f"unsupported pacman call {args}"

    def _yay(self, args: list[str]) -> tuple[int, str, str]:
        if args[0] == "-Sia":
            found = self.aur_packages.get(args[1])
            if found is None:
                return 1, "", ""
            version, description, votes = found
            return (
                0,
                (
                    f"Repository                    : aur\nName                          : {args[1]}\n"
                    f"Version                       : {version}\n"
                    f"Description                   : {description}\n"
                    f"Votes                         : {votes}\n"
                ),
                "",
            )
        if args[0] == "-Ssa":
            terms = [t.lower() for t in args[1:] if not t.startswith("-") and t != "votes"]
            lines = [
                f"aur/{name} {version} (+{votes} 1.00)\n    {description}\n"
                for name, (version, description, votes) in self.aur_packages.items()
                if all(t in f"{name} {description}".lower() for t in terms)
            ]
            return (0, "".join(lines), "") if lines else (1, "", "")
        return 1, "", f"unsupported yay call {args}"

    def _git(self, args: list[str]) -> tuple[int, str, str]:
        folder = args[args.index("-C") + 1]
        repo = self.git_repos.get(Path(folder).name)
        if repo is None:
            return 128, "", "fatal: not a git repository (or any of the parent directories): .git"
        command = next(a for a in args[args.index("-C") + 2 :] if not a.startswith("-"))
        if command == "status":
            return 0, repo["status"], ""
        if command == "diff":
            return 0, repo["numstat"], ""
        if command == "log":
            return 0, repo["log"], ""
        return 1, "", f"unsupported git call {args}"

    def _gh(self, args: list[str]) -> tuple[int, str, str]:
        return self.github.handle(args)

    def _kitty(self, args: list[str]) -> tuple[int, str, str]:
        return self.kitty.handle(args, self.last_input)

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
            if "\t" in "".join(args):  # title, artist, track id
                artist, track = self.song_details.get(name, ("", ""))
                return 0, f"{player[0]}\t{artist}\t{track}\n", ""
            return 0, player[0] + "\n", ""
        if args == ["play"]:
            player[1] = "Playing"
        if args[:1] == ["open"] and name == "spotify":  # plays the top result of a search, like Spotify
            query = unquote(args[1].removeprefix("spotify:search:"))
            title, artist = self.spotify_results.get(query, (query.title(), "Some Artist"))
            player[:] = [title, "Playing"]
            self.song_details[name] = (artist, f"/com/spotify/track/{zlib.crc32(query.encode()):08x}")
        return 0, "", ""

    def _gio(self, args: list[str]) -> tuple[int, str, str]:
        """gio trash: into ``trash_dir`` (files/ + info/, like the real one), --list, --restore."""
        if args[:1] != ["trash"] or self.trash_dir is None:
            return 1, "", "gio: not faked"
        files, info = self.trash_dir / "files", self.trash_dir / "info"
        if args[1:2] == ["--list"]:
            lines = []
            for entry in sorted(info.glob("*.trashinfo")) if info.is_dir() else []:
                original = entry.read_text().split("Path=", 1)[1].splitlines()[0]
                lines.append(f"trash:///{quote(entry.stem)}\t{unquote(original)}\n")
            return 0, "".join(lines), ""
        if args[1:2] == ["--restore"]:
            name = unquote(args[2].removeprefix("trash:///"))
            note = info / f"{name}.trashinfo"
            if not note.is_file():
                return 1, "", "gio: no such file in the trash"
            back = Path(unquote(note.read_text().split("Path=", 1)[1].splitlines()[0]))
            if back.exists():
                return 1, "", f"gio: {back}: file exists"
            back.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(files / name, back)
            note.unlink()
            return 0, "", ""
        path = Path(args[-1])
        if not path.exists():
            return 1, "", f"gio: {path}: No such file or directory"
        files.mkdir(parents=True, exist_ok=True)
        info.mkdir(parents=True, exist_ok=True)
        name, n = path.name, 1
        while (files / name).exists():
            n += 1
            name = f"{path.stem}.{n}{path.suffix}"
        shutil.move(path, files / name)
        (info / f"{name}.trashinfo").write_text(
            f"[Trash Info]\nPath={quote(str(path))}\nDeletionDate=2026-10-11T12:00:00\n"
        )
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
        echo = {"node.name": "echo-cancel-capture", "media.class": "Stream/Input/Audio"}  # SONU's own
        recording = [{"state": "running", "props": echo}] + [
            {"state": "running", "props": {"media.class": "Stream/Input/Audio", "application.name": app}}
            for app in self.mic_users
        ]
        nodes += [
            {"id": 90 + i, "type": "PipeWire:Interface:Node", "info": info}
            for i, info in enumerate(recording)
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
        if "--failed" in args:  # D81: services that failed (system, or the user's with --user)
            units = self.failed_user_units if "--user" in args else self.failed_units
            return 0, "".join(f"{unit} loaded failed failed {unit}\n" for unit in units), ""
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

        Path(args[-1]).write_bytes(PNG + str(self.screen_changes).encode())  # changes after a click/keys
        self.shots.append(args[-1])
        return 0, "", ""

    def _wlrctl(self, args: list[str]) -> tuple[int, str, str]:
        self.clicks.append(" ".join(args))
        if self.click_changes_screen:
            self.screen_changes += 1
        return 0, "", ""

    def _wtype(self, args: list[str]) -> tuple[int, str, str]:
        self.typed.append(self.last_input.decode() if args == ["-"] and self.last_input else " ".join(args))
        self.screen_changes += 1
        return 0, "", ""

    def _slurp(self, args: list[str]) -> tuple[int, str, str]:
        return (0, self.area + "\n", "") if self.area else (1, "", "selection cancelled\n")

    def _wl_copy(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["--clear"]:
            self.clipboard_type, self.clipboard_text = None, None
            return 0, "", ""
        kind = args[args.index("--type") + 1] if "--type" in args else "text/plain"
        if kind.startswith("text/"):
            self.clipboard_type, self.clipboard_text = None, (self.last_input or b"").decode()
        else:
            self.clipboard_type, self.clipboard_text = kind.split(";")[0], None
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

    def _orrery_theme(self, args: list[str]) -> tuple[int, str, str]:
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
                return 1, "", f"orrery-theme: no such theme: {args[1]}\n"
            self.theme = args[1]
        elif args[0] == "next":
            index = self.theme_names.index(self.theme)
            self.theme = self.theme_names[(index + 1) % len(self.theme_names)]
        elif args[0] == "toggle":  # orrery-theme prefers the sibling that differs by -light
            sibling = (
                self.theme.removesuffix("-light") if self.theme.endswith("-light") else f"{self.theme}-light"
            )
            self.theme = sibling if sibling in self.theme_names else self.theme_names[0]
        return 0, "", ""

    _hypr_theme = _orrery_theme  # the rice's names before it was renamed Orrery

    def _orrery_wall(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["current"]:
            return 0, (self.wallpaper or "") + "\n", ""
        if args[0] == "set":
            self.wallpaper = args[1]
        elif args[0] == "next" and self.backgrounds:
            index = self.backgrounds.index(self.wallpaper) if self.wallpaper in self.backgrounds else -1
            self.wallpaper = self.backgrounds[(index + 1) % len(self.backgrounds)]
        return 0, "", ""

    _hypr_wall = _orrery_wall

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
        if any(a.startswith("--query-compute-apps") for a in args):
            return 0, "".join(f"{name}, {mib}\n" for name, mib in self.gpu_apps), ""
        return 0, self.gpu_line + "\n", ""

    def _pdftotext(self, args: list[str]) -> tuple[int, str, str]:
        """A fake PDF is its text (D82): "converted" as it is."""
        return 0, Path(args[-2]).read_text(), ""

    def _soffice(self, args: list[str]) -> tuple[int, str, str]:
        """LibreOffice's conversion to text (D82): a fake document is its text, written like LibreOffice does
        (with a byte-order mark first); the profile it's given is recorded."""
        source = Path(args[-1])
        out = Path(args[args.index("--outdir") + 1])
        self.office_profiles.append(next(a for a in args if a.startswith("-env:UserInstallation=")))
        (out / f"{source.stem}.txt").write_text("\ufeff" + source.read_text(), encoding="utf-8")
        return 0, "", ""

    def _coredumpctl(self, args: list[str]) -> tuple[int, str, str]:
        rows = [
            {"time": int(when * 1e6), "exe": exe, "sig": sig, "pid": 1} for when, exe, sig in self.crashed
        ]
        return (0, json.dumps(rows), "") if rows else (1, "", "No coredumps found.\n")

    def _journalctl(self, args: list[str]) -> tuple[int, str, str]:
        logged = self.user_errors if "--user" in args else self.errors
        return (
            0,
            "".join(json.dumps({"SYSLOG_IDENTIFIER": who, "MESSAGE": text}) + "\n" for who, text in logged),
            "",
        )

    def _powerprofilesctl(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["get"]:
            return 0, self.power_mode + "\n", ""
        if args[:1] == ["set"]:
            if args[1] not in self.power_modes:
                return 1, "", f"Failed to activate profile '{args[1]}'\n"
            self.power_mode = args[1]
            return 0, "", ""
        return 1, "", "unsupported\n"
