"""Heads-ups (D88): SONU speaks up by itself, the way a person in the room would — "battery's at 14%, time to
plug in" — only when it's turned on (``[heads_up] enabled``), and only for what's worth it.

* What: the battery running low (and again when it's critical), the disk nearly full, the CPU staying very
  hot. Each is said **once** — the battery again only after it was charged, the disk once a day, the heat
  once an hour — never every minute.
* How: shown on the HUD (or as a notification). Said out loud only with ``speak = true``, and only when the
  user isn't busy: never while another app records the microphone (a call, OBS), never over a fullscreen
  window (a film, a game) or in the quiet hours — except a critical battery, which matters more than the
  film.
* From SONU's own read-only checks (the battery, the disk, the CPU temperature): nothing leaves the laptop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sonu.config.settings import HeadsUpSettings
from sonu.system import SystemControl

HOT_CHECKS = 2  # the CPU must be this hot this many checks in a row (a short burst isn't worth a word)
HOT_EVERY_S = 3600.0
GB = 1024**3


@dataclass(frozen=True, slots=True)
class HeadsUp:
    key: str  # "battery-low", "battery-critical", "disk", "hot"
    text: str
    urgent: bool = False  # said even over a fullscreen window and in the quiet hours (never during a call)


@dataclass
class Watch:
    """What has been said already, so each heads-up comes once."""

    settings: HeadsUpSettings
    said: dict[str, float] = field(default_factory=dict)  # key → when (time.time())
    disk_day: str = ""
    hot_checks: int = 0

    def check(self, system: SystemControl, now: datetime) -> list[HeadsUp]:
        found: list[HeadsUp] = []
        found += self._battery(system)
        found += self._disk(system, now)
        found += self._heat(system, now.timestamp())
        for heads_up in found:
            self.said[heads_up.key] = now.timestamp()
        return found

    def _battery(self, system: SystemControl) -> list[HeadsUp]:
        batteries = system.batteries()
        if not batteries:
            return []
        battery = batteries[0]
        if battery.status != "Discharging":
            self.said.pop("battery-low", None)  # plugged in: the next time it runs low is worth saying again
            self.said.pop("battery-critical", None)
            return []
        percent, s = battery.percent, self.settings
        if percent <= s.battery_critical and "battery-critical" not in self.said:
            self.said.setdefault("battery-low", 0.0)  # past "low" already: don't say both
            text = f"Battery's at {percent}% — plug in now, or the laptop will shut down soon."
            return [HeadsUp("battery-critical", text, urgent=True)]
        if percent <= s.battery_low and "battery-low" not in self.said:
            return [HeadsUp("battery-low", f"Battery's at {percent}% — time to plug in.")]
        return []

    def _disk(self, system: SystemControl, now: datetime) -> list[HeadsUp]:
        day = now.date().isoformat()
        if self.disk_day == day or (disk := system.disk()) is None:
            return []
        free, total = disk
        used = round(100 * (total - free) / total) if total else 0
        if used < self.settings.disk_full:
            return []
        self.disk_day = day
        left = f"{free / GB:.0f} GB" if free >= GB else f"{free // 1024**2} MB"
        return [HeadsUp("disk", f"Your disk is {used}% full — {left} left.")]

    def _heat(self, system: SystemControl, now: float) -> list[HeadsUp]:
        temperature = system.cpu_temperature()
        self.hot_checks = (
            self.hot_checks + 1 if temperature is not None and temperature >= self.settings.hot else 0
        )
        if self.hot_checks < HOT_CHECKS or now - self.said.get("hot", -HOT_EVERY_S) < HOT_EVERY_S:
            return []
        text = f"The CPU has been at {temperature:.0f} °C for a while — ask me what's using it."
        return [HeadsUp("hot", text)]


def in_quiet_hours(quiet: str, now: datetime) -> bool:
    """``"23:00-07:00"`` (may wrap past midnight); "" = none."""
    if not quiet:
        return False
    start, _, end = quiet.partition("-")
    minute = now.hour * 60 + now.minute
    first, last = (int(t[:2]) * 60 + int(t[3:]) for t in (start, end))
    return first <= minute < last if first <= last else minute >= first or minute < last


def busy(mic_users: list[str], fullscreen: bool, quiet: bool, heads_up: HeadsUp) -> str | None:
    """Why SONU mustn't say it out loud now (None: it may)."""
    if mic_users:
        return f"{mic_users[0]} is using the microphone"
    if heads_up.urgent:
        return None
    if fullscreen:
        return "a window is fullscreen"
    if quiet:
        return "it's the quiet hours"
    return None
