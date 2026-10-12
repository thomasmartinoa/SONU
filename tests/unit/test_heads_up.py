"""Heads-ups (D88): what SONU says by itself, once each, and when it keeps quiet."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sonu.config.settings import HeadsUpSettings
from sonu.core.heads_up import HeadsUp, Watch, busy, in_quiet_hours
from sonu.system import SystemControl
from sonu.system.control import parse_mic_users
from sonu.testing.fake_system import FakeSystem

NOW = datetime(2026, 10, 12, 15, 0)


class Laptop:
    def __init__(self, tmp_path: Path) -> None:
        self.battery = tmp_path / "power_supply" / "BAT1"
        self.battery.mkdir(parents=True)
        self.zone = tmp_path / "sys" / "class" / "thermal" / "thermal_zone0"
        self.zone.mkdir(parents=True)
        (self.zone / "type").write_text("x86_pkg_temp\n")
        self.set(80, "Discharging", 60)
        self.system = SystemControl(
            FakeSystem(), power_supply=tmp_path / "power_supply", sys=tmp_path / "sys", home=tmp_path
        )

    def set(self, percent: int, status: str = "Discharging", celsius: int = 60) -> None:
        (self.battery / "capacity").write_text(f"{percent}\n")
        (self.battery / "status").write_text(f"{status}\n")
        (self.zone / "temp").write_text(f"{celsius * 1000}\n")


def keys(found: list[HeadsUp]) -> list[str]:
    return [heads_up.key for heads_up in found]


def test_the_battery_once_low_once_critical_again_after_charging(tmp_path: Path) -> None:
    laptop, watch = Laptop(tmp_path), Watch(HeadsUpSettings(disk_full=100))
    assert watch.check(laptop.system, NOW) == []
    laptop.set(14)
    [low] = watch.check(laptop.system, NOW)
    assert low.text == "Battery's at 14% — time to plug in." and not low.urgent
    laptop.set(12)
    assert watch.check(laptop.system, NOW) == []  # said once, not every minute
    laptop.set(6)
    [critical] = watch.check(laptop.system, NOW)
    assert critical.key == "battery-critical" and critical.urgent
    laptop.set(40, "Charging")
    assert watch.check(laptop.system, NOW) == []
    laptop.set(15)
    assert keys(watch.check(laptop.system, NOW)) == ["battery-low"]  # unplugged and low again: worth saying


def test_straight_to_critical_is_said_once(tmp_path: Path) -> None:
    laptop, watch = Laptop(tmp_path), Watch(HeadsUpSettings(disk_full=100))
    laptop.set(5)
    assert keys(watch.check(laptop.system, NOW)) == ["battery-critical"]
    laptop.set(4)
    assert watch.check(laptop.system, NOW) == []


def test_heat_that_lasts_once_an_hour(tmp_path: Path) -> None:
    laptop, watch = Laptop(tmp_path), Watch(HeadsUpSettings(disk_full=100))
    laptop.set(80, celsius=95)
    assert watch.check(laptop.system, NOW) == []  # a short burst isn't worth a word
    [hot] = watch.check(laptop.system, NOW)
    assert hot.text == "The CPU has been at 95 °C for a while — ask me what's using it."
    later = datetime(2026, 10, 12, 15, 30)
    assert watch.check(laptop.system, later) == []
    assert keys(watch.check(laptop.system, datetime(2026, 10, 12, 16, 1))) == ["hot"]


def test_the_disk_once_a_day(tmp_path: Path) -> None:
    laptop, watch = Laptop(tmp_path), Watch(HeadsUpSettings(disk_full=50))
    laptop.system.disk = lambda: (30 * 1024**3, 1000 * 1024**3)  # type: ignore[method-assign]
    [disk] = watch.check(laptop.system, NOW)
    assert disk.text == "Your disk is 97% full — 30 GB left."
    assert watch.check(laptop.system, NOW) == []
    assert keys(watch.check(laptop.system, datetime(2026, 10, 13, 9, 0))) == ["disk"]


def test_when_sonu_keeps_quiet() -> None:
    plain, urgent = HeadsUp("battery-low", "…"), HeadsUp("battery-critical", "…", urgent=True)
    assert busy(["Discord"], False, False, urgent) == "Discord is using the microphone"  # never during a call
    assert busy([], True, False, plain) == "a window is fullscreen"
    assert busy([], False, True, plain) == "it's the quiet hours"
    assert busy([], True, True, urgent) is None  # a critical battery matters more than the film
    assert busy([], False, False, plain) is None
    assert in_quiet_hours("23:00-07:00", datetime(2026, 10, 12, 23, 30))
    assert in_quiet_hours("23:00-07:00", datetime(2026, 10, 13, 6, 59))
    assert not in_quiet_hours("23:00-07:00", datetime(2026, 10, 12, 7, 0))
    assert not in_quiet_hours("", datetime(2026, 10, 12, 3, 0))


def test_who_records_the_microphone() -> None:
    """Found on the laptop: SONU's own echo canceller is always recording ("echo-cancel-capture")."""

    def node(state: str, **props: str) -> dict[str, object]:
        return {"info": {"state": state, "props": {"media.class": "Stream/Input/Audio", **props}}}

    dump = [
        node("running", **{"node.name": "echo-cancel-capture", "media.name": "Echo-Cancel Capture"}),
        node("running", **{"application.name": "pw-record", "application.process.binary": "pw-record"}),
        node("running", **{"application.name": "Discord"}),
        node("suspended", **{"application.name": "Zoom"}),
    ]
    assert parse_mic_users(json.dumps(dump)) == ["Discord"]
