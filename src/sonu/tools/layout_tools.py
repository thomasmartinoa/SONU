"""Saved layouts (D67): "save this layout as work", "restore work", "what layouts do I have?",
"forget the work layout".

Saving records which app is on which workspace (and a terminal's folder) — apps SONU can start
again, never window titles. Restoring arranges the desktop like that: windows already open are moved
to their workspace (one already there stays), the missing ones are started there; every window is
checked on its workspace. Restoring never closes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.layouts import MAX_WINDOWS, Layout, Layouts, Slot
from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.dialect import WorkspaceTarget
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.integrations.kitty import TITLE_PREFIX
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.desktop_tools import _move_and_verify
from sonu.tools.file_tools import home_relative, inside_home
from sonu.tools.grounding import ungrounded_words
from sonu.tools.project_tools import TERMINAL_FOLDER, shell_started_in, terminal_argv
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

_THIS_WORKSPACE = re.compile(r"\b(?:this|the current|my current|current) workspace\b")


def _layouts(ctx: ToolContext) -> Layouts:
    return ctx.layouts.fresh()


def _own_words(ctx: ToolContext, name: str) -> ToolOutcome | None:
    if missing := ungrounded_words(name, ctx.user_words or ctx.request_text):
        return ToolOutcome(
            StepStatus.FAILED, f"The layout's name must be your own words ({', '.join(missing)} isn't)"
        )
    return None


def _names(layouts: Layouts) -> str:
    return ", ".join(layout.name for layout in layouts.items)


def _entry_for(window: Window, ctx: ToolContext) -> AppEntry | None:
    """The app a window belongs to (one SONU can start again)."""
    for entry in ctx.resolver.catalog.entries:
        if entry.no_display or not entry.argv:
            continue
        if window_matches(entry, (window.window_class, window.initial_class), ctx.learned(entry)):
            return entry
    return None


def _is_sonus(window: Window) -> bool:
    return window.title.startswith(TITLE_PREFIX)  # SONU's own terminals (Claude Code, a command's window)


def _terminal_folder(window: Window, entry: AppEntry, ctx: ToolContext) -> str:
    """The folder a terminal's shell is in: one folder inside the home folder (not the home itself)."""
    if entry.executable not in TERMINAL_FOLDER:
        return ""
    home = ctx.system.home
    folders = {f for f in ctx.system.child_folders(window.pid) if f != home and inside_home(f, home)}
    return str(folders.pop()) if len(folders) == 1 else ""


def _workspace_order(window: Window) -> tuple[int, int, int]:
    return (window.workspace.id, window.at[0], window.at[1])


# ---------------------------------------------------------------------------- layouts.save
class SaveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40, description="the layout's name, the user's words ('work')")


class SaveLayout(Tool):
    name = "layouts.save"
    description = (
        "Save the current window layout under a name: which app is on which workspace (the whole desktop; "
        "only this workspace when the user says so). 'Save this layout as work'."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.DESKTOP_READ, Effect.SETTINGS_CHANGE})
    Args = SaveArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SaveArgs, args)
        name = " ".join(a.name.split())
        if problem := _own_words(ctx, name):
            return problem
        # Found on the laptop (dry run): the model set "this workspace" for "save this setup as coding".
        # The user's words decide.
        this_workspace = bool(_THIS_WORKSPACE.search(normalize(ctx.request_text)))
        active = snapshot.active_workspace_id
        windows = [
            w
            for w in sorted(snapshot.windows, key=_workspace_order)
            if w.workspace.id >= 1 and not _is_sonus(w) and (not this_workspace or w.workspace.id == active)
        ]
        slots: list[Slot] = []
        unknown: list[str] = []
        for window in windows:
            entry = _entry_for(window, ctx)
            if entry is None:
                unknown.append(window.window_class)
                continue
            folder = _terminal_folder(window, entry, ctx)
            slots.append(Slot(entry.desktop_id, entry.name, window.workspace.name, folder))
        if not slots:
            where = "on this workspace" if this_workspace else "open"
            return ToolOutcome(StepStatus.FAILED, f"There are no windows {where} that SONU could open again")
        layout = Layout(name, tuple(slots[:MAX_WINDOWS]))
        notes = []
        if len(slots) > MAX_WINDOWS:
            notes.append(f"only the first {MAX_WINDOWS} windows")
        if unknown:
            notes.append(f"not {', '.join(sorted(set(unknown)))} — no app SONU can start")
        note = f" ({'; '.join(notes)})" if notes else ""
        existing = _layouts(ctx).get(name)
        if existing is not None:
            return Prepared(
                f"Replace the layout '{existing.name}' ({existing.describe()}) with: "
                f"{layout.describe()}{note}",
                layout,
                risk=Risk.MEDIUM,  # the old one is gone after this
            )
        return Prepared(f"Save the layout '{name}': {layout.describe()}{note}", layout)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        layout = cast(Layout, prepared.payload)
        layouts = _layouts(ctx)
        layouts.put(layout)
        stored = _layouts(ctx).get(layout.name)
        check = Check("the layout is saved", stored is not None and stored.slots == layout.slots)
        if not check.passed:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't save the layout '{layout.name}'", (check,))
        folders = sum(1 for slot in layout.slots if slot.folder)
        kept = f" — with {folders} terminal folder{'s' if folders > 1 else ''}" if folders else ""
        return ToolOutcome(
            StepStatus.DONE, f"Saved the layout '{layout.name}': {layout.describe()}{kept}", (check,)
        )


# ---------------------------------------------------------------------------- layouts.restore
@dataclass(frozen=True, slots=True)
class _Step:
    slot: Slot
    entry: AppEntry | None
    window: Window | None  # an open window to use (None: start the app)
    folder: Path | None = None


def _plan_restore(layout: Layout, ctx: ToolContext, snapshot: WorldSnapshot) -> list[_Step]:
    """Which open window each slot uses: first one of its app already on its workspace, then any other
    (a terminal only if its shell is in the saved folder); otherwise the app is started."""
    free = [w for w in snapshot.windows if w.workspace.id >= 1 and not _is_sonus(w)]
    entries = {slot.app: ctx.resolver.catalog.by_id(slot.app) for slot in layout.slots}
    folders: dict[str, set[Path]] = {}

    def fits(window: Window, slot: Slot) -> bool:
        entry = entries[slot.app]
        if entry is None or not window_matches(
            entry, (window.window_class, window.initial_class), ctx.learned(entry)
        ):
            return False
        if slot.folder:
            if window.address not in folders:
                folders[window.address] = ctx.system.child_folders(window.pid)
            return Path(slot.folder) in folders[window.address]
        return True

    chosen: dict[int, Window] = {}
    for in_place in (True, False):
        for index, slot in enumerate(layout.slots):
            if index in chosen:
                continue
            window = next(
                (w for w in free if fits(w, slot) and (not in_place or w.workspace.name == slot.workspace)),
                None,
            )
            if window is not None:
                chosen[index] = window
                free.remove(window)
    steps = []
    for index, slot in enumerate(layout.slots):
        folder = Path(slot.folder) if slot.folder else None
        if folder is not None and not (folder.is_dir() and inside_home(folder, ctx.system.home)):
            folder = None  # the folder is gone: the terminal opens in its usual place
        steps.append(_Step(slot, entries[slot.app], chosen.get(index), folder))
    return steps


def _describe_steps(steps: list[_Step], ctx: ToolContext) -> str:
    parts = []
    for step in steps:
        name, where = step.slot.app_name, step.slot.workspace
        if step.entry is None:
            parts.append(f"{name} isn't installed anymore")
        elif step.window is None:
            folder = f" in {home_relative(step.folder, ctx.system.home)}" if step.folder else ""
            parts.append(f"open {name}{folder} on {where}")
        elif step.window.workspace.name != where:
            parts.append(f"move {name} to {where}")
    in_place = sum(1 for s in steps if s.window is not None and s.window.workspace.name == s.slot.workspace)
    if in_place:
        parts.append(f"{in_place} already in place")
    return "; ".join(parts)


class RestoreArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40, description="the layout's name, the user's words")


class RestoreLayout(Tool):
    name = "layouts.restore"
    description = (
        "Arrange the desktop like a saved layout: move the open windows to their workspaces and open the "
        "missing apps there. 'Restore work', 'set up my work layout'. Closes nothing."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH, Effect.WINDOW_MOVE})
    Args = RestoreArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        name = cast(RestoreArgs, args).name
        if problem := _own_words(ctx, name):
            return problem
        layouts = _layouts(ctx)
        layout = layouts.get(name) or layouts.get(name.removesuffix(" layout"))
        if layout is None:
            have = f" — you have: {_names(layouts)}" if layouts.items else " — you haven't saved any yet"
            return ToolOutcome(StepStatus.FAILED, f"There's no layout called '{name}'{have}")
        steps = _plan_restore(layout, ctx, snapshot)
        return Prepared(f"Restore the layout '{layout.name}': {_describe_steps(steps, ctx)}", (layout, steps))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        layout, steps = cast("tuple[Layout, list[_Step]]", prepared.payload)
        checks: list[Check] = []
        problems: list[str] = []
        windows: list[str] = []
        used = {step.window.address for step in steps if step.window is not None}
        for step in steps:  # moves first: then the new windows are the only ones left to tell apart
            if step.window is None or step.entry is None:
                continue
            windows.append(step.window.address)
            target = WorkspaceTarget.of(step.slot.workspace)
            if step.window.workspace.name == step.slot.workspace:
                checks.append(Check(f"{step.slot.app_name} is on {step.slot.workspace}", True))
                continue
            moved = await _move_and_verify(ctx, step.window, target, step.slot.app_name)
            if isinstance(moved, ToolOutcome):
                problems.append(f"couldn't move {step.slot.app_name}")
                continue
            checks += moved
            if not all(check.passed for check in moved):
                problems.append(f"{step.slot.app_name} didn't move to {step.slot.workspace}")
        for step in steps:
            if step.entry is None:
                problems.append(f"{step.slot.app_name} isn't installed anymore")
            elif step.window is None:
                window = await self._open(step, step.entry, ctx, used, checks, problems)
                if window is not None:
                    used.add(window.address)
                    windows.append(window.address)
        done = f"Restored the layout '{layout.name}': {layout.describe()}"
        if not problems and checks and all(check.passed for check in checks):
            return ToolOutcome(StepStatus.DONE, done, tuple(checks), data={"windows": windows})
        return ToolOutcome(
            StepStatus.FAILED,
            f"Restored the layout '{layout.name}' only in part: {'; '.join(problems) or 'a check failed'}",
            tuple(checks),
            data={"windows": windows},
        )

    @staticmethod
    async def _open(
        step: _Step,
        entry: AppEntry,
        ctx: ToolContext,
        used: set[str],
        checks: list[Check],
        problems: list[str],
    ) -> Window | None:
        name, workspace = step.slot.app_name, step.slot.workspace
        target = WorkspaceTarget.of(workspace)
        argv = terminal_argv(entry, step.folder) if step.folder else ctx.launch_argv(entry)
        if argv is None:
            problems.append(f"{name} can't be started")
            return None
        before = await ctx.desktop.snapshot()
        learned = ctx.learned(entry)
        try:
            await ctx.desktop.launch(argv, target)
        except HyprlandError:
            problems.append(f"couldn't start {name}")
            return None

        def opened(snapshot: WorldSnapshot) -> Window | None:
            return next(
                (
                    w
                    for w in snapshot.new_windows(before)
                    if w.address not in used
                    and window_matches(entry, (w.window_class, w.initial_class), learned)
                ),
                None,
            )

        window, _ = await ctx.desktop.wait_for(opened, timeout_s=ctx.settings.apps.launch_timeout_s)
        if window is None:
            problems.append(f"{name} didn't open a window")
            return None
        if window.workspace.name != workspace:  # single-instance apps ignore the launch rule
            moved = await _move_and_verify(ctx, window, target, name)
            if isinstance(moved, ToolOutcome) or not all(check.passed for check in moved):
                problems.append(f"{name} opened on {window.workspace.name}, not {workspace}")
                return window
            checks += moved
        else:
            checks.append(Check(f"{name} opened on {workspace}", True))
        if step.folder is not None:
            where = home_relative(step.folder, ctx.system.home)
            in_folder = await shell_started_in(ctx, window.pid, step.folder)
            checks.append(Check(f"{name}'s shell is in {where}", in_folder))
            if not in_folder:
                problems.append(f"couldn't see {name}'s shell start in {where}")
        return window


# ---------------------------------------------------------------------------- layouts.list / forget
class ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListLayouts(Tool):
    name = "layouts.list"
    description = "The saved layouts and what's in each ('what layouts do I have?')."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ListArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        return Prepared("List the saved layouts")

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        layouts = _layouts(ctx)
        read = (Check("read the saved layouts", True),)
        if not layouts.items:
            return ToolOutcome(StepStatus.DONE, "You have no saved layouts", read)
        listed = "; ".join(f"{layout.name}: {layout.describe()}" for layout in layouts.items)
        return ToolOutcome(StepStatus.DONE, f"Your layouts — {listed}", read)


class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40, description="the layout's name, the user's words")


class ForgetLayout(Tool):
    name = "layouts.forget"
    description = "Delete a saved layout by its name ('forget the work layout')."
    risk = Risk.LOW
    effects = frozenset({Effect.SETTINGS_CHANGE})
    Args = ForgetArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        name = cast(ForgetArgs, args).name
        if problem := _own_words(ctx, name):
            return problem
        layouts = _layouts(ctx)
        layout = layouts.get(name) or layouts.get(name.removesuffix(" layout"))
        if layout is None:
            return ToolOutcome(StepStatus.FAILED, f"There's no layout called '{name}'")
        return Prepared(f"Delete the layout '{layout.name}' ({layout.describe()})", layout.name)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        name = cast(str, prepared.payload)
        _layouts(ctx).remove(name)
        gone = Check("the layout is gone", _layouts(ctx).get(name) is None)
        return ToolOutcome(
            StepStatus.DONE if gone.passed else StepStatus.FAILED,
            f"Deleted the layout '{name}'" if gone.passed else f"Couldn't delete the layout '{name}'",
            (gone,),
        )


LAYOUT_TOOLS: tuple[Tool, ...] = (SaveLayout(), RestoreLayout(), ListLayouts(), ForgetLayout())
