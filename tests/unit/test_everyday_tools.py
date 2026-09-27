from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from sonu.config.settings import AppsSettings, Settings, VerifySettings, WebSettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SubprocessRunner, SystemControl
from sonu.system.commands import CommandError
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, app_pid, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import StepStatus
from sonu.tools.context import ToolContext
from sonu.tools.media_tools import same_title
from sonu.tools.registry import ToolRegistry

NOW = datetime(2026, 9, 25, 17, 45, 0)


@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    home: Path
    confirmations: list[ConfirmRequest] = field(default_factory=list)

    async def run(
        self, request: str, *steps: tuple[str, Mapping[str, object]], dry_run: bool = False
    ) -> TaskReport:
        async def approve(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return True

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*((tool, dict(args)) for tool, args in steps))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=approve, dry_run=dry_run)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    system = FakeSystem()
    fake_hypr.on_url = system.page_opened
    fake_hypr.apps["zen-browser"] = FakeApp("zen", "Zen Browser")
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "none"),
    }
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    for zone, (kind, milli) in enumerate((("acpitz", "48000"), ("x86_pkg_temp", "63000"))):
        path = tmp_path / "sys" / "class" / "thermal" / f"thermal_zone{zone}"
        path.mkdir(parents=True)
        (path / "type").write_text(kind + "\n")
        (path / "temp").write_text(milli + "\n")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal:       32768000 kB\nMemFree: 1 kB\nMemAvailable:   22528000 kB\n")
    (proc / "uptime").write_text("11520.55 90000.00\n")
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(catalog, role_defaults={"browser": "zen-browser"}),
        Settings(
            apps=AppsSettings(launch_timeout_s=1.5),
            verify=VerifySettings(timeout_s=0.5),
            web=WebSettings(load_timeout_s=2.0),
        ),
        which=lambda name: f"/usr/bin/{name}",
        ancestors=frozenset,
        system=SystemControl(system, proc=proc, sys=tmp_path / "sys", home=tmp_path),
        now=lambda: NOW,
    )
    yield Rig(fake_hypr, system, ctx, tmp_path)


# ---------------------------------------------------------------- media.play
def test_same_title() -> None:
    assert same_title("Alan Walker - Faded", "Alan Walker - Faded (Official Music Video)")
    assert same_title("(3) Alan Walker - Faded - YouTube", "Alan Walker - Faded")
    assert not same_title("Some Other Song", "Alan Walker - Faded")
    assert same_title("東京フラッシュ", "東京フラッシュ")  # no ASCII letters: compared as is


@pytest.mark.anyio
async def test_video_is_found_opened_pressed_play_and_checked(rig: Rig) -> None:
    rig.system.videos["faded"] = ("60ItHLz5WEA", "Alan Walker - Faded")
    report = await rig.run("play faded on youtube", ("media.play", {"query": "faded"}))
    assert report.lines() == ["✔ Playing 'Alan Walker - Faded' on YouTube in Zen Browser"]
    assert rig.fake.launched[-1] == ["/usr/bin/zen-browser", "https://www.youtube.com/watch?v=60ItHLz5WEA"]
    assert ("playerctl", "-p", "firefox.instance_1_42", "play") in rig.system.calls  # autoplay was held back
    assert rig.system.videos_playing == 1


@pytest.mark.anyio
async def test_browser_without_media_controls_is_unverified(rig: Rig) -> None:
    # Found on real hardware: Zen opened the video paused (autoplay blocked) and wasn't in the media
    # controls, so SONU could neither press play nor hear it.
    rig.system.browser_mpris = False
    report = await rig.run("play faded", ("media.play", {"query": "faded"}))
    outcome = report.steps[0].outcome
    assert outcome.status is StepStatus.UNVERIFIED
    assert (
        "couldn't confirm it's playing (no sound from Zen Browser, and Zen Browser isn't in"
        in outcome.summary
    )
    assert "allowing autoplay for youtube.com in its site settings" in outcome.summary


@pytest.mark.anyio
async def test_sound_from_the_browser_confirms_it_plays(rig: Rig) -> None:
    # Found on real hardware: Chromium played the video but never appeared in the media controls.
    rig.system.browser_mpris = False
    rig.system.browser_autoplay = True
    rig.system.videos["faded"] = ("60ItHLz5WEA", "Alan Walker - Faded")
    report = await rig.run("play faded", ("media.play", {"query": "faded"}))
    assert report.lines() == ["✔ Playing 'Alan Walker - Faded' on YouTube in Zen Browser"]
    checks = {check.description: check for check in report.steps[0].outcome.checks}
    assert checks["Zen Browser started playing sound"].evidence["pid"] == app_pid("zen")
    assert rig.system.videos_playing == 1


@pytest.mark.anyio
async def test_sound_that_was_already_playing_doesnt_count(rig: Rig) -> None:
    rig.system.browser_mpris = False
    rig.system.streams.append([7, app_pid("zen"), True])  # music in another tab, before SONU's page
    report = await rig.run("play faded", ("media.play", {"query": "faded"}))
    assert report.steps[0].outcome.status is StepStatus.UNVERIFIED


@pytest.mark.anyio
async def test_sound_from_a_browser_helper_process_counts(rig: Rig) -> None:
    # Browsers play sound from a child process (Chromium's audio service), not the window's.
    rig.system.browser_mpris = False
    child = 4242
    (rig.ctx.system.proc / str(child)).mkdir()
    (rig.ctx.system.proc / str(child) / "stat").write_text(f"{child} (chromium) S {app_pid('zen')} 1 1\n")
    (rig.ctx.system.proc / str(app_pid("zen"))).mkdir()
    (rig.ctx.system.proc / str(app_pid("zen")) / "stat").write_text(f"{app_pid('zen')} (zen-bin) S 1 1 1\n")

    def autoplays_in_a_helper(url: str, window_class: str) -> None:
        rig.system.page_opened(url, window_class)
        rig.system.streams.append([8, child, True])

    rig.fake.on_url = autoplays_in_a_helper
    report = await rig.run("play faded", ("media.play", {"query": "faded"}))
    assert report.steps[0].outcome.status is StepStatus.DONE


@pytest.mark.anyio
async def test_sound_from_another_app_doesnt_count(rig: Rig) -> None:
    rig.system.browser_mpris = False

    def other_app_starts(url: str, window_class: str) -> None:
        rig.system.page_opened(url, window_class)
        rig.system.streams.append([9, app_pid("spotify"), True])

    rig.fake.on_url = other_app_starts
    report = await rig.run("play faded", ("media.play", {"query": "faded"}))
    assert report.steps[0].outcome.status is StepStatus.UNVERIFIED


@pytest.mark.anyio
async def test_the_browser_the_user_named_is_used_when_the_plan_leaves_it_out(rig: Rig) -> None:
    # Found on real hardware: "play faded on youtube in firefox" was planned without a browser and
    # played in Zen (the default) — reported as done.
    report = await rig.run("play faded in chromium", ("media.play", {"query": "faded"}), dry_run=True)
    assert report.lines() == ["· would: Play 'faded' from YouTube in Chromium"]
    # Two browsers named: SONU can't tell which one this step is for, so the default stays.
    report = await rig.run("play faded in chromium or zen", ("media.play", {"query": "faded"}), dry_run=True)
    assert report.lines() == ["· would: Play 'faded' from YouTube in Zen Browser"]


@pytest.mark.anyio
async def test_play_needs_yt_dlp_and_the_users_words(rig: Rig) -> None:
    report = await rig.run("play faded", ("media.play", {"query": "my private notes"}))
    assert "isn't in your request" in report.lines()[0]
    rig.system.installed.discard("yt-dlp")
    report = await rig.run("play faded", ("media.play", {"query": "faded"}))
    assert report.lines() == [
        "✖ yt-dlp isn't installed (install: sudo pacman -S yt-dlp) — SONU uses it to find the video to play"
    ]
    assert not any(call[0] == "yt-dlp" for call in rig.system.calls)


@pytest.mark.anyio
async def test_dry_run_sends_nothing_to_youtube(rig: Rig) -> None:
    report = await rig.run("play faded", ("media.play", {"query": "faded"}), dry_run=True)
    assert report.lines() == ["· would: Play 'faded' from YouTube in Zen Browser"]
    assert rig.system.calls == [] and rig.fake.launched == []


# ---------------------------------------------------------------- reminders.set
@pytest.mark.anyio
async def test_reminder_in_minutes(rig: Rig) -> None:
    report = await rig.run("remind me", ("reminders.set", {"text": "check the oven", "in_minutes": 15}))
    assert report.lines() == ["✔ I'll remind you at 18:00 (in 15 min): check the oven"]
    [(unit, text)] = rig.system.timers.items()
    assert unit.startswith("sonu-reminder-20260925-180000-") and text == "check the oven"
    call = next(c for c in rig.system.calls if c[0] == "systemd-run")
    assert "--on-calendar=2026-09-25 18:00:00" in call and call[-3:] == (
        "--",
        "SONU reminder",
        "check the oven",
    )


@pytest.mark.anyio
async def test_reminder_at_a_time_that_passed_is_tomorrow(rig: Rig) -> None:
    report = await rig.run("remind me", ("reminders.set", {"text": "call mom", "at": "9:30"}))
    assert report.lines() == ["✔ I'll remind you at 09:30 tomorrow (in 15 h 45 min): call mom"]


def test_reminder_needs_exactly_one_time() -> None:
    from sonu.tools.everyday_tools import ReminderArgs

    with pytest.raises(ValueError, match="exactly one of in_minutes or at"):
        ReminderArgs.model_validate({"text": "x", "in_minutes": 5, "at": "10:00"})
    with pytest.raises(ValueError, match="24-hour time"):
        ReminderArgs.model_validate({"text": "x", "at": "7pm"})


# ---------------------------------------------------------------- reminders.list / cancel
@pytest.mark.anyio
async def test_list_and_cancel_reminders(rig: Rig) -> None:
    report = await rig.run("what reminders do I have", ("reminders.list", {}))
    assert report.lines() == ["✔ No reminders are set"]
    await rig.run(
        "r",
        ("reminders.set", {"text": "check the oven", "in_minutes": 15}),
        ("reminders.set", {"text": "call mum", "at": "18:30"}),
    )
    report = await rig.run("what reminders do I have", ("reminders.list", {}))
    assert report.lines() == [
        "✔ 2 reminders: 18:00 (in 15 min): check the oven · 18:30 (in 45 min): call mum"
    ]
    assert report.steps[0].outcome.private
    report = await rig.run("cancel the oven reminder", ("reminders.cancel", {"about": "oven"}))
    assert report.lines() == ["✔ Cancelled 'check the oven'"]
    assert list(rig.system.timers.values()) == ["call mum"]


@pytest.mark.anyio
async def test_cancel_asks_which_when_it_cant_tell(rig: Rig) -> None:
    await rig.run(
        "r",
        ("reminders.set", {"text": "stretch", "in_minutes": 20}),
        ("reminders.set", {"text": "drink water", "in_minutes": 40}),
    )
    report = await rig.run("cancel my reminder", ("reminders.cancel", {}))
    assert report.status == "needs-input"
    assert rig.system.reminders == 2
    report = await rig.run("cancel all my reminders", ("reminders.cancel", {"all": True}))
    assert report.lines() == ["✔ Cancelled 2 reminders"]
    assert rig.system.reminders == 0
    report = await rig.run("cancel the gym reminder", ("reminders.cancel", {"about": "gym"}))
    assert report.lines() == ["✖ No reminders are set"]


@pytest.mark.anyio
async def test_only_sonus_own_timers_can_be_stopped() -> None:
    with pytest.raises(CommandError, match="isn't one of SONU's reminders"):
        await SystemControl(FakeSystem()).cancel_reminder("dbus-broker")


# ---------------------------------------------------------------- screen.capture
@pytest.mark.anyio
async def test_screenshot_of_the_focused_window_like_the_rice(rig: Rig) -> None:
    (rig.home / "Pictures" / "screenshot").mkdir(parents=True)  # the rice's folder
    window = make_window("0xa1", "kitty", "zsh", stable_id="a1")
    window["at"], window["size"] = [12, 34], [800, 600]
    rig.fake.windows = [window]
    rig.fake.active_address = "0xa1"
    report = await rig.run("screenshot", ("screen.capture", {"what": "window"}))
    assert report.lines() == [
        "✔ Saved a screenshot to ~/Pictures/screenshot/screenshot-2026-09-25_17-45-00.png and copied it"
    ]
    grim = next(c for c in rig.system.calls if c[0] == "grim")
    assert grim[1:3] == ("-g", "12,34 800x600")


@pytest.mark.anyio
async def test_screenshot_area_cancelled_changes_nothing(rig: Rig) -> None:
    rig.system.area = None
    report = await rig.run("screenshot", ("screen.capture", {"what": "area"}))
    assert report.steps[0].outcome.status is StepStatus.DENIED
    assert rig.system.screenshots == 0


@pytest.mark.anyio
async def test_screenshot_without_wl_copy_still_saves(rig: Rig) -> None:
    rig.system.installed.discard("wl-copy")
    report = await rig.run("screenshot", ("screen.capture", {}))
    assert report.status == "done"
    assert "wl-copy isn't installed" in report.lines()[0]
    assert (rig.home / "Pictures" / "Screenshots").is_dir()


# ---------------------------------------------------------------- desktop.shell
@pytest.mark.anyio
async def test_rice_switches(rig: Rig) -> None:
    report = await rig.run(
        "r",
        ("desktop.shell", {"action": "night_light_on"}),
        ("desktop.shell", {"action": "keep_awake_on"}),
        ("desktop.shell", {"action": "clear_notifications"}),
        ("desktop.shell", {"action": "lock_screen"}),
    )
    assert report.lines() == [
        "✔ Night light is on",
        "✔ Keep awake is on",
        "✔ Cleared all notifications",
        "✔ Locked the screen",
    ]
    assert rig.system.night_light == "on" and rig.system.keep_awake == "on" and rig.system.notifications == 0


@pytest.mark.anyio
async def test_do_not_disturb_reaches_the_asked_state_whatever_it_was(rig: Rig) -> None:
    rig.system.shell["dnd"] = "true"  # already on: the rice's function toggles, so SONU toggles back
    report = await rig.run("r", ("desktop.shell", {"action": "do_not_disturb_on"}))
    assert report.lines() == ["✔ Do-not-disturb is on"] and rig.system.do_not_disturb
    report = await rig.run("r", ("desktop.shell", {"action": "do_not_disturb_off"}))
    assert report.lines() == ["✔ Do-not-disturb is off"] and not rig.system.do_not_disturb


@pytest.mark.anyio
async def test_night_light_off_is_asked_again_when_the_rice_shows_the_old_state(rig: Rig) -> None:
    # Found on real hardware: "turn off the night light" stopped hyprsunset, but the rice's status
    # said "on" for up to 10 s and SONU reported "night light is on".
    rig.system.shell["nightlight"] = "on"
    rig.system.stale_once.add("nightlight")
    report = await rig.run("r", ("desktop.shell", {"action": "night_light_off"}))
    assert report.lines() == ["✔ Night light is off"]
    assert rig.system.night_light == "off"


@pytest.mark.anyio
async def test_night_light_needs_hyprsunset(rig: Rig) -> None:
    async def not_installed(target: str, function: str) -> str:
        return "hyprsunset not installed" if function == "status" else ""

    rig.ctx.system.shell_call = not_installed  # type: ignore[method-assign]
    report = await rig.run("r", ("desktop.shell", {"action": "night_light_on"}))
    assert report.lines() == ["✖ The night light needs hyprsunset"]


# ---------------------------------------------------------------- laptop health
@pytest.mark.anyio
async def test_health_status(rig: Rig) -> None:
    report = await rig.run("r", ("system.status", {"what": ["health"]}))
    summary = report.steps[0].outcome.summary
    assert "Memory: 9.8 of 31.2 GiB used" in summary
    assert "CPU temperature: 63 °C" in summary  # the CPU package sensor, not the hottest zone
    assert "GPU: 12% busy · 55 °C · 4.0 of 8.0 GiB" in summary
    assert "Up 3 h 12 min" in summary
    assert "Disk:" in summary and "CPU:" in summary


# ---------------------------------------------------------------- the real runner
@pytest.mark.anyio
async def test_runner_passes_input_and_can_skip_capturing() -> None:
    runner = SubprocessRunner()
    result = await runner.run(["cat"], input_bytes=b"hello")
    assert result.stdout == "hello"
    result = await runner.run(["sh", "-c", "echo hidden; exit 0"], capture=False)
    assert result.ok and result.stdout == ""
