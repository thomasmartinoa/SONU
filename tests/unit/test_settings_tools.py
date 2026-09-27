from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sonu.config.settings import Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import (
    ProgramMissing,
    SubprocessRunner,
    SystemControl,
    parse_backlight,
    parse_bluetooth_powered,
    parse_volume,
    pick_backlight,
)
from sonu.system.commands import CommandError, program_env
from sonu.system.control import AudioStream, parse_audio_streams
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk, StepStatus
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


# ---------------------------------------------------------------- parsing real program output
def test_parse_volume() -> None:
    assert parse_volume("Volume: 0.40\n") == parse_volume("Volume: 0.40")
    assert parse_volume("Volume: 0.40\n").percent == 40
    assert parse_volume("Volume: 0.65 [MUTED]\n").muted
    with pytest.raises(Exception, match="Unexpected wpctl output"):
        parse_volume("Translate error")


def test_backlight_skips_nvidia_like_the_rice() -> None:
    listing = "nvidia_0,backlight,50,50%,100\nintel_backlight,backlight,48000,40%,120000\n"
    assert pick_backlight(listing) == "intel_backlight"
    assert pick_backlight("nvidia_0,backlight,50,50%,100\n") == "nvidia_0"  # dGPU-only mode
    assert pick_backlight("") is None
    backlight = parse_backlight("intel_backlight,backlight,48000,40%,120000")
    assert backlight is not None and backlight.percent == 40


def test_parse_bluetooth_powered() -> None:
    assert parse_bluetooth_powered("Controller 00:11 (public)\n\tPowered: yes\n") is True
    assert (
        parse_bluetooth_powered("Controller 00:11 (public)\n\tPowered: no\n\tPowerState: off-blocked")
        is False
    )
    assert parse_bluetooth_powered("No default controller available\n") is None


@pytest.mark.anyio
async def test_real_runner_uses_argv_c_locale_and_explains_missing_programs() -> None:
    runner = SubprocessRunner()
    result = await runner.run(["sh", "-c", 'echo "$LC_ALL"; exit 3'])
    assert (result.stdout.strip(), result.returncode) == ("C", 3)
    assert result.problem() == "sh failed: C"
    with pytest.raises(
        ProgramMissing, match=r"wpctl isn't installed \(install: sudo pacman -S wireplumber\)"
    ):
        await SubprocessRunner(which=lambda name: None).run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])


def test_parse_audio_streams() -> None:
    dump = """[
      {"id": 0, "type": "PipeWire:Interface:Core", "info": {"props": {}}},
      {"id": 91, "type": "PipeWire:Interface:Node", "info": {"state": "idle", "props": {
        "media.class": "Stream/Output/Audio", "object.serial": 545, "application.process.id": 249505}}},
      {"id": 92, "type": "PipeWire:Interface:Node", "info": {"state": "running", "props": {
        "media.class": "Stream/Output/Audio", "object.serial": 612, "application.process.id": "330600"}}},
      {"id": 93, "type": "PipeWire:Interface:Node", "info": {"state": "running", "props": {
        "media.class": "Audio/Sink", "object.serial": 60}}},
      {"id": 94, "type": "PipeWire:Interface:Link", "info": null}
    ]"""
    assert parse_audio_streams(dump) == [AudioStream(545, 249505, False), AudioStream(612, 330600, True)]
    with pytest.raises(CommandError, match="Unexpected pw-dump output"):
        parse_audio_streams("not json")


def test_programs_run_without_sonus_virtualenv() -> None:
    # Found on real hardware: `uv run sonu "how is my laptop doing?"` → "Power mode: unknown
    # (powerprofilesctl failed: ModuleNotFoundError: No module named 'gi')".
    environ = {"PATH": "/home/u/SONU/.venv/bin:/usr/local/bin:/usr/bin", "VIRTUAL_ENV": "/home/u/SONU/.venv"}
    env = program_env(environ, prefix="/home/u/SONU/.venv", base_prefix="/usr")
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert "VIRTUAL_ENV" not in env
    assert (env["LC_ALL"], env["LANG"]) == ("C", "C")
    # Not in a virtualenv (the system's Python): PATH is left alone.
    assert program_env(environ, prefix="/usr", base_prefix="/usr")["PATH"] == environ["PATH"]


# ---------------------------------------------------------------- the tools, end to end
@dataclass
class Confirmations:
    answer: bool
    seen: list[ConfirmRequest] = field(default_factory=list)

    async def __call__(self, request: ConfirmRequest) -> bool:
        self.seen.append(request)
        return self.answer


@dataclass
class Rig:
    system: FakeSystem
    ctx: ToolContext

    async def run(
        self, *steps: tuple[str, Mapping[str, object]], confirm: bool = True, dry_run: bool = False
    ) -> tuple[TaskReport, Confirmations]:
        # The request is made of the steps' own words, so name grounding passes unless a test
        # runs a different request on purpose.
        request = " ".join(str(value) for _, args in steps for value in args.values()) or "r"
        confirmations = Confirmations(confirm)
        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*((tool, dict(args)) for tool, args in steps))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        report = await engine.run(request, confirm=confirmations, dry_run=dry_run)
        return report, confirmations


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    system = FakeSystem()
    battery = tmp_path / "power_supply" / "BAT1"
    battery.mkdir(parents=True)
    (battery / "capacity").write_text("76\n")
    (battery / "status").write_text("Discharging\n")
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    catalog = AppCatalog.load({"HOME": str(tmp_path), "XDG_DATA_DIRS": str(tmp_path / "none")})
    ctx = ToolContext(
        desktop,
        AppResolver(catalog),
        Settings(verify=VerifySettings(timeout_s=0.5)),
        ancestors=frozenset,
        system=SystemControl(system, power_supply=tmp_path / "power_supply"),
    )
    yield Rig(system, ctx)


@pytest.mark.anyio
async def test_bluetooth_is_unblocked_then_powered_on(rig: Rig) -> None:
    report, _ = await rig.run(("settings.bluetooth", {"on": True}))
    assert report.lines() == ["✔ Bluetooth is on"]
    assert rig.system.bluetooth_powered
    unblock = rig.system.calls.index(("rfkill", "unblock", "bluetooth"))
    assert unblock < rig.system.calls.index(("bluetoothctl", "power", "on"))


@pytest.mark.anyio
async def test_bluetooth_that_stays_off_is_reported_not_done(rig: Rig) -> None:
    rig.system.installed.discard("rfkill")  # can't unblock → bluez refuses to power on
    report, _ = await rig.run(("settings.bluetooth", {"on": True}))
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert "Bluetooth didn't turn on" in report.steps[0].outcome.summary


@pytest.mark.anyio
async def test_no_bluetooth_adapter(rig: Rig) -> None:
    rig.system.bluetooth_adapter = False
    report, _ = await rig.run(("settings.bluetooth", {"on": True}))
    assert report.lines() == ["✖ No Bluetooth adapter found"]


@pytest.mark.anyio
async def test_volume_is_set_capped_and_verified(rig: Rig) -> None:
    report, _ = await rig.run(("settings.volume", {"target": "speaker", "level": 30}))
    assert report.lines() == ["✔ Speaker volume is now 30%"]
    assert ("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "30%") in rig.system.calls
    rig.system.speaker = 0.95
    report, _ = await rig.run(("settings.volume", {"target": "speaker", "change": 20}))
    assert report.lines() == ["✔ Speaker volume is now 100%"]  # never above 100%


@pytest.mark.anyio
async def test_volume_change_while_muted_says_so(rig: Rig) -> None:
    rig.system.speaker_muted = True
    report, _ = await rig.run(("settings.volume", {"target": "speaker", "change": -10}))
    assert report.lines() == [
        "✔ Speaker volume is now 40% — it's muted, so you won't hear it (say 'unmute the speaker')"
    ]


@pytest.mark.anyio
async def test_mute_the_mic(rig: Rig) -> None:
    report, _ = await rig.run(("settings.volume", {"target": "mic", "mute": True}))
    assert report.lines() == ["✔ Mic muted"]
    assert rig.system.mic_muted and not rig.system.speaker_muted


def test_volume_needs_exactly_one_instruction() -> None:
    from sonu.tools.settings_tools import VolumeArgs

    with pytest.raises(ValueError, match="exactly one of level, change, mute"):
        VolumeArgs.model_validate({"target": "speaker", "level": 30, "mute": True})
    with pytest.raises(ValueError, match="exactly one"):
        VolumeArgs.model_validate({"target": "mic"})
    with pytest.raises(ValueError, match="target"):  # speaker or mic must always be said
        VolumeArgs.model_validate({"mute": True})


@pytest.mark.anyio
async def test_brightness_never_goes_black_and_leaves_nvidia_alone(rig: Rig) -> None:
    report, _ = await rig.run(("settings.brightness", {"level": 0}))
    assert report.lines() == [
        "✔ Screen brightness is now 5% (SONU keeps at least 5% so the screen stays visible)"
    ]
    assert rig.system.backlights["nvidia_0"] == (50, 100)
    report, _ = await rig.run(("settings.brightness", {"change": 25}))
    assert rig.system.brightness == 30
    # The rice's popup shows each new level, as its brightness keys do (volume's shows by itself).
    assert rig.system.osd == ["5", "30"]


@pytest.mark.anyio
async def test_turning_wifi_off_asks_first(rig: Rig) -> None:
    def wifi() -> bool:  # read through a call so the checks below aren't narrowed away
        return rig.system.wifi

    report, confirmations = await rig.run(("settings.wifi", {"on": False}), confirm=False)
    assert [request.risk for request in confirmations.seen] == [Risk.MEDIUM]
    assert report.status == "declined" and wifi()
    report, confirmations = await rig.run(("settings.wifi", {"on": False}), confirm=True)
    assert report.lines() == ["✔ Wi-Fi is off"] and not wifi()
    report, confirmations = await rig.run(("settings.wifi", {"on": True}))
    assert confirmations.seen == [] and wifi()  # turning it on doesn't ask


@pytest.mark.anyio
async def test_media_control(rig: Rig) -> None:
    report, _ = await rig.run(("media.control", {"action": "pause"}))
    assert report.lines() == ["✔ Media paused"]
    report, _ = await rig.run(("media.control", {"action": "next"}))
    assert report.lines() == ["✔ Skipped to the next track"]
    rig.system.track = len(rig.system.tracks) - 1  # last track: the player doesn't move on
    report, _ = await rig.run(("media.control", {"action": "next"}))
    assert report.steps[0].outcome.status is StepStatus.UNVERIFIED
    rig.system.player = None
    report, _ = await rig.run(("media.control", {"action": "play"}))
    assert report.lines() == ["✖ No media player is running"]


@pytest.mark.anyio
async def test_power_mode(rig: Rig) -> None:
    report, _ = await rig.run(("settings.power_mode", {"mode": "power-saver"}))
    assert report.lines() == ["✔ Power mode is power-saver"]
    rig.system.power_modes = ("balanced", "power-saver")  # e.g. no performance mode on this laptop
    report, _ = await rig.run(("settings.power_mode", {"mode": "performance"}))
    assert report.lines() == ["✖ powerprofilesctl failed: Failed to activate profile 'performance'"]


@pytest.mark.anyio
async def test_missing_program_says_what_to_install(rig: Rig) -> None:
    rig.system.installed.discard("wpctl")
    report, _ = await rig.run(("settings.volume", {"target": "speaker", "level": 30}))
    assert report.lines() == ["✖ wpctl isn't installed (install: sudo pacman -S wireplumber)"]


@pytest.mark.anyio
async def test_status_reads_everything_it_can(rig: Rig) -> None:
    rig.system.installed.discard("powerprofilesctl")
    report, _ = await rig.run(("system.status", {}))
    assert report.status == "done"
    assert report.steps[0].outcome.summary == (
        "Volume: 50% · Mic: 80% · Brightness: 40% · Bluetooth: off · Wi-Fi: on (Home: 5G) · "
        "Media: playing — Artist One Song · "
        "Power mode: unknown (powerprofilesctl isn't installed) · Battery: 76% (discharging)"
    )
    assert all(call[0] != "wpctl" or call[1] == "get-volume" for call in rig.system.calls)  # read-only
    assert report.steps[0].outcome.private  # "what's playing" is the user's own listening


@pytest.mark.anyio
async def test_dry_run_changes_no_setting(rig: Rig) -> None:
    report, _ = await rig.run(
        ("settings.volume", {"target": "speaker", "level": 10}),
        ("settings.bluetooth", {"on": True}),
        dry_run=True,
    )
    assert report.lines() == ["· would: Set the speaker volume to 10%", "· would: Turn Bluetooth on"]
    assert rig.system.calls == []


@pytest.mark.anyio
async def test_bluetooth_service_not_running_is_explained(rig: Rig) -> None:
    rig.system.bluetooth_service = False
    report, _ = await rig.run(("settings.bluetooth", {"on": True}))
    [line] = report.lines()
    assert line.startswith("✖ bluetoothctl didn't finish within 5s")
    assert line.endswith("is the Bluetooth service running? (systemctl start bluetooth)")


# ---------------------------------------------------------------- Wi-Fi networks and Bluetooth devices
def test_nmcli_terse_fields_unescape_colons() -> None:
    from sonu.system.control import split_terse

    assert split_terse(r"Home\: 5G:802-11-wireless") == ["Home: 5G", "802-11-wireless"]
    assert split_terse(r"back\\slash:802-11-wireless") == ["back\\slash", "802-11-wireless"]


def test_matching_names_from_the_users_words() -> None:
    from sonu.tools.settings_tools import match_networks

    saved = ["Home: 5G", "hithere", "Office 2G", "Office 5G"]
    assert match_networks("hi there", saved) == ["hithere"]
    assert match_networks("home", saved) == ["Home: 5G"]
    assert match_networks("office wifi", saved) == ["Office 2G", "Office 5G"]
    assert match_networks("cafe", saved) == []


@pytest.mark.anyio
async def test_switching_to_a_saved_network_asks_first(rig: Rig) -> None:
    request = ("settings.wifi_connect", {"network": "hithere"})
    report, confirmations = await rig.run(request, confirm=False)
    assert [r.description for r in confirmations.seen] == ["Switch Wi-Fi from 'Home: 5G' to 'hithere'"]
    assert report.status == "declined" and rig.system.wifi_connection == "Home: 5G"
    report, _ = await rig.run(request, confirm=True)
    assert report.lines() == ["✔ Connected to 'hithere'"]
    assert rig.system.wifi_connection == "hithere"


@pytest.mark.anyio
async def test_connecting_with_wifi_off_turns_it_on_without_asking(rig: Rig) -> None:
    rig.system.wifi, rig.system.wifi_connection = False, None
    report, confirmations = await rig.run(("settings.wifi_connect", {"network": "hithere"}))
    assert confirmations.seen == []  # nothing to lose: no current network
    assert report.lines() == ["✔ Connected to 'hithere'"]


@pytest.mark.anyio
async def test_unknown_or_ungrounded_networks_are_refused(rig: Rig) -> None:
    report, _ = await rig.run(("settings.wifi_connect", {"network": "cafe"}))
    assert "isn't a saved Wi-Fi network" in report.lines()[0]
    # Engine sets the request per task; here the request never mentioned "hithere".
    planner_step = ("settings.wifi_connect", {"network": "hithere"})
    engine = Engine(
        planner=ScriptedPlanner({"move the browser to 3": steps_plan(planner_step)}),
        registry=ToolRegistry(),
        ctx=rig.ctx,
        policy=Policy("confirm-risky"),
    )
    report = await engine.run("move the browser to 3", confirm=Confirmations(True))
    assert report.lines() == ["✖ Won't use the network 'hithere': hithere isn't in your request"]
    assert rig.system.wifi_connection == "Home: 5G"


@pytest.mark.anyio
async def test_headphones_are_found_by_kind_and_bluetooth_is_turned_on(rig: Rig) -> None:
    report, _ = await rig.run(("settings.bluetooth_device", {"device": "headphones"}))
    assert report.lines() == ["✔ WH-1000XM4 is connected"]
    assert rig.system.bluetooth_powered and rig.system.bt_connected == ["WH-1000XM4"]


@pytest.mark.anyio
async def test_bluetooth_device_by_name_and_disconnect(rig: Rig) -> None:
    rig.system.bluetooth_blocked, rig.system.bluetooth_powered = False, True
    report, _ = await rig.run(("settings.bluetooth_device", {"device": "mx keys"}))
    assert report.lines() == ["✔ MX Keys is connected"]
    report, _ = await rig.run(("settings.bluetooth_device", {"device": "mx keys", "connect": False}))
    assert report.lines() == ["✔ MX Keys is disconnected"] and rig.system.bt_connected == []


@pytest.mark.anyio
async def test_bluetooth_device_not_paired(rig: Rig) -> None:
    report, _ = await rig.run(("settings.bluetooth_device", {"device": "speaker"}))
    assert report.lines()[0].startswith("✖ No paired Bluetooth device matches 'speaker' — pair it once")
