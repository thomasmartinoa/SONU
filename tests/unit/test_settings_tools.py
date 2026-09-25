from __future__ import annotations

from collections.abc import AsyncIterator
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
        self, *steps: tuple[str, dict[str, object]], confirm: bool = True, dry_run: bool = False
    ) -> tuple[TaskReport, Confirmations]:
        confirmations = Confirmations(confirm)
        engine = Engine(
            planner=ScriptedPlanner({"r": steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        report = await engine.run("r", confirm=confirmations, dry_run=dry_run)
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
    report, _ = await rig.run(("settings.volume", {"level": 30}))
    assert report.lines() == ["✔ Speaker volume is now 30%"]
    assert ("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "30%") in rig.system.calls
    rig.system.speaker = 0.95
    report, _ = await rig.run(("settings.volume", {"change": 20}))
    assert report.lines() == ["✔ Speaker volume is now 100%"]  # never above 100%


@pytest.mark.anyio
async def test_volume_change_while_muted_says_so(rig: Rig) -> None:
    rig.system.speaker_muted = True
    report, _ = await rig.run(("settings.volume", {"change": -10}))
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
        VolumeArgs.model_validate({"level": 30, "mute": True})
    with pytest.raises(ValueError, match="exactly one"):
        VolumeArgs.model_validate({})


@pytest.mark.anyio
async def test_brightness_never_goes_black_and_leaves_nvidia_alone(rig: Rig) -> None:
    report, _ = await rig.run(("settings.brightness", {"level": 0}))
    assert report.lines() == [
        "✔ Screen brightness is now 5% (SONU keeps at least 5% so the screen stays visible)"
    ]
    assert rig.system.backlights["nvidia_0"] == (50, 100)
    report, _ = await rig.run(("settings.brightness", {"change": 25}))
    assert rig.system.brightness == 30


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
    report, _ = await rig.run(("settings.volume", {"level": 30}))
    assert report.lines() == ["✖ wpctl isn't installed (install: sudo pacman -S wireplumber)"]


@pytest.mark.anyio
async def test_status_reads_everything_it_can(rig: Rig) -> None:
    rig.system.installed.discard("powerprofilesctl")
    report, _ = await rig.run(("system.status", {}))
    assert report.status == "done"
    assert report.steps[0].outcome.summary == (
        "Volume: 50% · Mic: 80% · Brightness: 40% · Bluetooth: off · Wi-Fi: on · Media: playing · "
        "Power mode: unknown (powerprofilesctl isn't installed) · Battery: 76% (discharging)"
    )
    assert all(call[0] != "wpctl" or call[1] == "get-volume" for call in rig.system.calls)  # read-only


@pytest.mark.anyio
async def test_dry_run_changes_no_setting(rig: Rig) -> None:
    report, _ = await rig.run(
        ("settings.volume", {"level": 10}), ("settings.bluetooth", {"on": True}), dry_run=True
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
