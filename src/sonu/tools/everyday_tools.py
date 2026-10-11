"""Everyday helpers: reminders and timers, screenshots, and the rice's own switches.

- ``reminders.set`` schedules a systemd user timer that shows a notification; it's checked by
  asking systemd that the timer is active.
- ``screen.capture`` saves a PNG like the rice's screenshot keys do (``~/Pictures/screenshot``) and
  can copy it; the file is checked to exist and be a real PNG.
- ``desktop.shell`` flips the switches the owner's Quickshell shell offers over its IPC (night light,
  keep awake, do-not-disturb, clear notifications, lock) and reads each one's state back.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sonu.desktop.apps.resolver import normalize
from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import Reminder
from sonu.tools.base import (
    ALL_CHOICE,
    Choice,
    Choices,
    Effect,
    Prepared,
    Risk,
    StepStatus,
    Tool,
    ToolOutcome,
    outcome_from_checks,
)
from sonu.tools.context import ToolContext
from sonu.tools.grounding import CONNECTIVES
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _missing(ctx: ToolContext, *programs: str) -> ToolOutcome | None:
    for program in programs:
        if not ctx.system.available(program):
            return ToolOutcome(StepStatus.FAILED, str(ProgramMissing(program)))
    return None


async def _settle[T](
    read: Callable[[], Awaitable[T]],
    ok: Callable[[T], bool],
    ctx: ToolContext,
    *,
    within_s: float | None = None,
) -> T:
    """Re-read a switch's state until it's what we asked for (e.g. hyprsunset takes a moment)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (ctx.settings.verify.timeout_s if within_s is None else within_s)
    while True:
        value = await read()
        if ok(value) or loop.time() >= deadline:
            return value
        await asyncio.sleep(0.2)


def _home_relative(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------- reminders.set
class ReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=120, description="what to remind about, in the user's words")
    in_minutes: int | None = Field(default=None, ge=1, le=10080, description="remind after this many minutes")
    at: str | None = Field(default=None, description="or at this time today (24-hour HH:MM, e.g. 18:30)")

    @model_validator(mode="after")
    def _when(self) -> Self:
        if (self.in_minutes is None) == (self.at is None):
            raise ValueError("give exactly one of in_minutes or at")
        if self.at is not None and not _CLOCK.match(self.at):
            raise ValueError("at must be a 24-hour time like 18:30")
        return self


def _when(a: ReminderArgs, now: datetime) -> datetime:
    if a.in_minutes is not None:
        return (now + timedelta(minutes=a.in_minutes)).replace(microsecond=0)
    hour, minute = (int(part) for part in (a.at or "0:0").split(":"))
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def _in_words(delta: timedelta) -> str:
    minutes = max(1, round(delta.total_seconds() / 60))
    if minutes < 60:
        return f"in {minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"in {hours} h {rest} min" if rest else f"in {hours} h"


class SetReminder(Tool):
    name = "reminders.set"
    description = (
        "Remind the user of something later, or set a timer ('in 20 minutes', 'at 18:30'): a notification."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SCHEDULE})
    Args = ReminderArgs
    # Found in an eval: "call me Martin" was planned as the prompt's reminder example ("stretch"): the
    # planner now sends a reminder whose words aren't the user's back to the model.
    grounded = ("text",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ReminderArgs, args)
        if problem := _missing(ctx, "systemd-run", "systemctl", "notify-send"):
            return problem
        now = ctx.now()
        when = _when(a, now)
        day = "" if when.date() == now.date() else " tomorrow"
        text = " ".join(a.text.split())
        return Prepared(f"Remind you at {when:%H:%M}{day} ({_in_words(when - now)}): {text}", (when, text))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        when, text = cast(tuple[datetime, str], prepared.payload)
        unit = f"sonu-reminder-{when:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        try:
            await ctx.system.schedule_reminder(unit, when, text)
            active = await ctx.system.timer_active(unit)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, str(exc))
        check = Check("the reminder is scheduled", active, {"unit": unit, "at": f"{when:%Y-%m-%d %H:%M}"})
        now = ctx.now()
        day = "" if when.date() == now.date() else " tomorrow"
        return outcome_from_checks(
            [check],
            f"I'll remind you at {when:%H:%M}{day} ({_in_words(when - now)}): {text}",
            "The reminder didn't get scheduled",
        )


# ---------------------------------------------------------------------------- screen.capture
class CaptureArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: Literal["screen", "window", "area"] = Field(
        default="screen",
        description="screen (everything), window (the focused one) or area (the user drags one)",
    )
    clipboard: bool = Field(default=True, description="also copy the picture to the clipboard")


class CaptureScreen(Tool):
    name = "screen.capture"
    description = "Take a screenshot of the screen, the focused window or an area, saved and copied."
    risk = Risk.LOW
    effects = frozenset({Effect.SCREEN_CAPTURE})
    Args = CaptureArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(CaptureArgs, args)
        if problem := _missing(ctx, "grim", *(("slurp",) if a.what == "area" else ())):
            return problem
        geometry: str | None = None
        label = "the screen"
        if a.what == "window":
            window = snapshot.active_window
            if window is None:
                return ToolOutcome(StepStatus.FAILED, "No window is focused")
            geometry = f"{window.at[0]},{window.at[1]} {window.size[0]}x{window.size[1]}"
            label = f"the {window.window_class} window"
        elif a.what == "area":
            label = "an area you select"
        name = f"screenshot-{ctx.now():%Y-%m-%d_%H-%M-%S}.png"  # the rice's naming
        path = ctx.system.screenshot_dir() / name
        return Prepared(f"Take a screenshot of {label}", (a, geometry, path))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        a, geometry, path = cast(tuple[CaptureArgs, str | None, Path], prepared.payload)
        home = ctx.system.home
        try:
            if a.what == "area":
                geometry = await ctx.system.select_area()
                if geometry is None:
                    return ToolOutcome(StepStatus.DENIED, "No area was selected — nothing captured")
            await ctx.system.screenshot(path, geometry)
            header = await asyncio.to_thread(_png_header, path)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, str(exc))
        saved = Check("the screenshot was saved", header, {"file": _home_relative(path, home)})
        checks = [saved]
        note = ""
        if a.clipboard and header:
            if ctx.system.available("wl-copy"):
                try:
                    await ctx.system.copy_image(path)
                    copied = "image/png" in await ctx.system.clipboard_types()
                except CommandError:
                    copied = False
                checks.append(Check("the screenshot is on the clipboard", copied, {"copied": copied}))
                note = " and copied it" if copied else ""
            else:
                note = f" ({ProgramMissing('wl-copy')}, so it isn't copied)"
        return outcome_from_checks(
            checks,
            f"Saved a screenshot to {_home_relative(path, home)}{note}",
            f"The screenshot wasn't saved to {_home_relative(path, home)}",
        )


def _png_header(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            return file.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


# ---------------------------------------------------------------------------- desktop.shell
ShellAction = Literal[
    "night_light_on",
    "night_light_off",
    "keep_awake_on",
    "keep_awake_off",
    "do_not_disturb_on",
    "do_not_disturb_off",
    "clear_notifications",
    "lock_screen",
]
# Switches with on/off/status functions on the rice's Quickshell IPC: action prefix → IPC target.
_SWITCHES = {"night_light": ("nightlight", "night light"), "keep_awake": ("caffeine", "keep awake")}


class ShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ShellAction = Field(
        description="night_light_on/off, keep_awake_on/off (screen never sleeps), do_not_disturb_on/off, "
        "clear_notifications or lock_screen"
    )


_CLEAR_SAID = re.compile(r"\b(?:clear|dismiss|delete|remove|get rid of|empty)\b", re.I)
_DND_SAID = re.compile(
    r"\b(?:turn|switch)\s+(on|off)\s+(?:the\s+)?do\s*-?\s*not\s*-?\s*disturb\b"
    r"|\bdo\s*-?\s*not\s*-?\s*disturb\s+(on|off)\b"
    r"|\b(enable|disable)\s+(?:the\s+)?do\s*-?\s*not\s*-?\s*disturb\b",
    re.I,
)


class DesktopShell(Tool):
    name = "desktop.shell"
    description = (
        "The desktop shell's switches: night light, keep awake (no screen sleep), do not disturb, "
        "clear notifications, lock the screen."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ShellArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        action = cast(ShellArgs, args).action
        if action == "clear_notifications" and not _CLEAR_SAID.search(ctx.request_text):
            # Found in the evals (v37; once before on the laptop): "turn off night light and turn off do not
            # disturb" cleared the notifications. Only "clear / dismiss …" clears them; do-not-disturb said
            # with on or off decides that switch instead.
            said = _DND_SAID.search(ctx.request_text)
            if said is None:
                return ToolOutcome(
                    StepStatus.FAILED, "You didn't ask to clear your notifications — none cleared"
                )
            state = next(g for g in said.groups() if g)
            action = "do_not_disturb_on" if state.lower() in ("on", "enable") else "do_not_disturb_off"
        if not ctx.system.available("qs"):
            return ToolOutcome(
                StepStatus.FAILED,
                f"{ProgramMissing('qs')} — these switches come from the rice's Quickshell shell",
            )
        text = {
            "night_light_on": "Turn the night light on",
            "night_light_off": "Turn the night light off",
            "keep_awake_on": "Keep the screen awake",
            "keep_awake_off": "Let the screen sleep again",
            "do_not_disturb_on": "Turn on do-not-disturb",
            "do_not_disturb_off": "Turn off do-not-disturb",
            "clear_notifications": "Clear all notifications",
            "lock_screen": "Lock the screen",
        }[action]
        return Prepared(text, action)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        action = cast(ShellAction, prepared.payload)
        try:
            return await self._run(action, ctx)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"The shell didn't accept it: {exc}")

    @staticmethod
    async def _run(action: ShellAction, ctx: ToolContext) -> ToolOutcome:
        call = ctx.system.shell_call
        prefix, _, state = action.rpartition("_")
        if prefix in _SWITCHES:
            target, label = _SWITCHES[prefix]
            await call(target, state)
            status = await _settle(
                lambda: call(target, "status"),
                lambda s: s == state,
                ctx,
                within_s=min(1.0, ctx.settings.verify.timeout_s / 2),
            )
            if status != state and "not installed" not in status:
                # Found on real hardware: the rice reads whether hyprsunset runs the moment "off"
                # returns, while hyprsunset is still exiting (~0.2 s), and then shows "on" until its
                # next poll 10 s later. On and off are idempotent; asking again makes it look afresh.
                await call(target, state)
                status = await _settle(lambda: call(target, "status"), lambda s: s == state, ctx)
            if "not installed" in status:
                return ToolOutcome(
                    StepStatus.FAILED, f"The {label} needs {status.replace(' not installed', '')}"
                )
            check = Check(f"{label} is {state}", status == state, {"status": status})
            return outcome_from_checks([check], f"{label.capitalize()} is {state}", f"{label} is {status}")
        if action.startswith("do_not_disturb"):
            wanted = "true" if action.endswith("_on") else "false"
            dnd = await call("notifications", "dnd")  # the rice's function toggles and returns the new state
            if dnd != wanted:
                dnd = await call("notifications", "dnd")
            word = "on" if wanted == "true" else "off"
            check = Check(f"do-not-disturb is {word}", dnd == wanted, {"dnd": dnd})
            return outcome_from_checks(
                [check], f"Do-not-disturb is {word}", f"Do-not-disturb didn't turn {word}"
            )
        if action == "clear_notifications":
            await call("notifications", "clear")
            count = await call("notifications", "count")
            check = Check("no notifications are left", count == "0", {"count": count})
            return outcome_from_checks(
                [check], "Cleared all notifications", f"{count} notifications are still there"
            )
        await call("lock", "lock")
        status = await _settle(lambda: call("lock", "status"), lambda s: s == "locked", ctx)
        check = Check("the screen is locked", status == "locked", {"status": status})
        return outcome_from_checks([check], "Locked the screen", "The screen didn't lock")


# ---------------------------------------------------------------------------- reminders.list / cancel
def _describe(reminder: Reminder, now: datetime) -> str:
    if reminder.when is None:
        return reminder.text
    day = "" if reminder.when.date() == now.date() else f" {reminder.when:%a %d %b}"
    return f"{reminder.when:%H:%M}{day} ({_in_words(reminder.when - now)}): {reminder.text}"


class ListRemindersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListReminders(Tool):
    name = "reminders.list"
    description = "Tell the user which reminders and timers are set (read-only)."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ListRemindersArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        if problem := _missing(ctx, "systemctl"):
            return problem
        return Prepared("List your reminders", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        try:
            reminders = await ctx.system.reminders()
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the reminders: {exc}")
        if not reminders:
            return ToolOutcome(StepStatus.DONE, "No reminders are set")
        now = ctx.now()
        return ToolOutcome(
            StepStatus.DONE,
            f"{len(reminders)} reminder{'s' if len(reminders) > 1 else ''}: "
            + " · ".join(_describe(reminder, now) for reminder in reminders),
            private=True,  # the user's own reminder texts
        )


class CancelReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    about: str | None = Field(
        default=None,
        max_length=120,
        description="words from the reminder, in the user's words ('the oven one')",
    )
    all: bool = Field(default=False, description="true only if the user said all their reminders")


def _matching(reminders: list[Reminder], about: str) -> list[Reminder]:
    """The reminders whose text shares the most words with ``about`` (none if no word matches)."""
    wanted = {word for word in normalize(about).split() if word not in CONNECTIVES}
    wanted -= {"reminder", "reminders", "timer", "timers", "one", "about", "remind", "me"}
    scored = [(len(wanted & set(normalize(r.text).split())), r) for r in reminders]
    best = max((score for score, _ in scored), default=0)
    return [r for score, r in scored if best and score == best]


class CancelReminder(Tool):
    name = "reminders.cancel"
    description = "Cancel a reminder or timer the user set (by words from it), or all of them."
    risk = Risk.LOW
    effects = frozenset({Effect.SCHEDULE})
    Args = CancelReminderArgs
    grounded = ("about",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(CancelReminderArgs, args)
        if problem := _missing(ctx, "systemctl"):
            return problem
        try:
            reminders = await ctx.system.reminders()
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the reminders: {exc}")
        if not reminders:
            return ToolOutcome(StepStatus.FAILED, "No reminders are set")
        now = ctx.now()
        if choice == ALL_CHOICE or a.all:
            chosen = reminders
        elif choice is not None:
            chosen = [r for r in reminders if r.unit == choice]
            if not chosen:
                return ToolOutcome(StepStatus.FAILED, "That reminder is no longer set")
        else:
            chosen = _matching(reminders, a.about) if a.about else reminders
            if not chosen:
                return ToolOutcome(StepStatus.FAILED, f"No reminder matches '{a.about}'")
            if len(chosen) > 1:
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT,
                    "several reminders match",
                    question="Which reminder should I cancel?",
                    choices=Choices(
                        "Which reminder should I cancel?",
                        (
                            *(Choice(_describe(r, now), r.unit) for r in chosen[:6]),
                            Choice(f"All {len(chosen)} of them", ALL_CHOICE),
                        ),
                    ),
                )
        label = _describe(chosen[0], now) if len(chosen) == 1 else f"all {len(chosen)} reminders"
        return Prepared(f"Cancel the reminder at {label}" if len(chosen) == 1 else f"Cancel {label}", chosen)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        chosen = cast(list[Reminder], prepared.payload)
        checks = []
        for reminder in chosen:
            try:
                await ctx.system.cancel_reminder(reminder.unit)
                active = await ctx.system.timer_active(reminder.unit)
            except CommandError as exc:
                return ToolOutcome(StepStatus.FAILED, f"Couldn't cancel it: {exc}")
            checks.append(Check(f"'{reminder.text}' is cancelled", not active, {"unit": reminder.unit}))
        what = f"'{chosen[0].text}'" if len(chosen) == 1 else f"{len(chosen)} reminders"
        return outcome_from_checks(checks, f"Cancelled {what}", f"{what} is still set")


EVERYDAY_TOOLS: tuple[Tool, ...] = (
    SetReminder(),
    ListReminders(),
    CancelReminder(),
    CaptureScreen(),
    DesktopShell(),
)
