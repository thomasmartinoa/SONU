"""The milestone-1 tools: open apps, move/focus/close windows, show workspaces, describe the desktop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.hyprland.dialect import WorkspaceTarget
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
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
from sonu.tools.targets import (
    WindowTarget,
    ambiguity_question,
    find_windows,
    narrow_by_request,
    wants_all,
    window_label,
)
from sonu.verify.checks import (
    Check,
    window_exists,
    window_focused,
    window_gone,
    window_on_workspace,
    workspace_active,
)
from sonu.world.snapshot import WindowRef, WorldSnapshot

WorkspaceArg = int | str


def _workspace(value: WorkspaceArg | None) -> WorkspaceTarget | ToolOutcome | None:
    if value is None:
        return None
    try:
        return WorkspaceTarget.of(value)
    except ValueError as exc:
        return ToolOutcome(StepStatus.FAILED, f"can't use workspace {value!r}: {exc}")


def _pick(
    windows: tuple[Window, ...], target: WindowTarget, all_: bool, ctx: ToolContext
) -> tuple[Window, ...]:
    """Narrow several matches with the user's own words, unless every match was asked for."""
    if all_ or len(windows) < 2:
        return windows
    return narrow_by_request(windows, ctx.request_text, target)


def _failed_dispatch(action: str, exc: HyprlandError) -> ToolOutcome:
    return ToolOutcome(StepStatus.FAILED, f"Hyprland refused to {action}: {exc}")


MAX_CHOICES = 6


def _resolve_app(app: str, ctx: ToolContext) -> AppEntry | ToolOutcome:
    resolution = ctx.resolver.resolve_for_request(app, ctx.request_text)
    if resolution.status == "ambiguous":
        candidates = resolution.candidates[:MAX_CHOICES]
        names = " or ".join(candidate.name for candidate in candidates[:4])
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            f"'{app}' could mean several apps",
            question=f"Which one did you mean for '{app}': {names}?",
            data={"candidates": [c.desktop_id for c in resolution.candidates]},
            choices=Choices(
                f"Which app did you mean by '{app}'?",
                tuple(Choice(candidate.name, candidate.desktop_id) for candidate in candidates),
            ),
        )
    if resolution.entry is None:
        return ToolOutcome(StepStatus.FAILED, f"'{app}' is not installed ({resolution.reason})")
    return resolution.entry


def _is_terminal(entry: AppEntry) -> bool:
    return "TerminalEmulator" in entry.categories


def _short(text: str, limit: int = 48) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chosen_app(desktop_id: str, ctx: ToolContext) -> AppEntry | ToolOutcome:
    entry = ctx.resolver.catalog.by_id(desktop_id)
    if entry is None:
        return ToolOutcome(StepStatus.FAILED, f"{desktop_id} is no longer installed")
    return entry


def _target_windows(
    target: WindowTarget,
    all_: bool,
    ctx: ToolContext,
    snapshot: WorldSnapshot,
    *,
    verb: str,
    choice: str | None,
    already_at: str | None = None,
) -> tuple[Window, ...] | ToolOutcome:
    """The window(s) a move/close step means — or a question with the matching windows as choices."""
    match = find_windows(target, snapshot, ctx)
    if match.problem:
        return ToolOutcome(StepStatus.FAILED, match.problem.capitalize())
    if choice == ALL_CHOICE:
        return match.windows
    if choice is not None:
        chosen = tuple(window for window in match.windows if window.address == choice)
        return chosen or ToolOutcome(StepStatus.FAILED, "The window you picked is no longer open")
    windows = _pick(match.windows, target, all_, ctx)
    if already_at is not None and len(windows) > 1:
        # Moving a window to where it already is changes nothing: only the others are candidates.
        elsewhere = tuple(w for w in windows if w.workspace.name != already_at)
        windows = elsewhere or windows
    if len(windows) > 1 and not (all_ or wants_all(ctx.request_text)):
        active = snapshot.active_window
        shown = windows[:MAX_CHOICES]
        options = tuple(
            Choice(
                window_label(w, focused=active is not None and w.address == active.address)
                + (f" — {_short(w.title)}" if w.title else ""),
                w.address,
            )
            for w in shown
        )
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            f"several windows match {target.describe()}",
            question=ambiguity_question(target, windows, verb),
            choices=Choices(
                f"Which window should I {verb}?",
                (*options, Choice(f"All {len(windows)} of them", ALL_CHOICE)),
            ),
        )
    return windows


def _require[V](value: V | None, what: str) -> V:
    if value is None:  # a bug in SONU, not a user error
        raise RuntimeError(f"internal error: missing {what}")
    return value


# ---------------------------------------------------------------------------- apps.launch
class LaunchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str = Field(
        min_length=1, max_length=80, description="the app as the user named it, e.g. 'VS Code', 'browser'"
    )
    workspace: WorkspaceArg | None = Field(default=None, description="workspace number or name to put it on")
    new_window: bool = Field(
        default=False, description="true only if the user asked for a new/another window"
    )


@dataclass(frozen=True, slots=True)
class _LaunchPlan:
    entry: AppEntry
    target: WorkspaceTarget | None
    mode: Literal["launch", "move-existing", "focus-existing", "already-there"]
    argv: tuple[str, ...] = ()
    window: Window | None = None


class LaunchApp(Tool):
    name = "apps.launch"
    description = (
        "Open an installed app, optionally on a workspace. If the app is already open, its existing window "
        "is moved there (or focused) instead, unless new_window is true. Terminals always open a new window."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH, Effect.WINDOW_MOVE, Effect.WINDOW_FOCUS})
    Args = LaunchArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(LaunchArgs, args)
        entry = _chosen_app(choice, ctx) if choice is not None else _resolve_app(a.app, ctx)
        if isinstance(entry, ToolOutcome):
            return entry
        target = _workspace(a.workspace)
        if isinstance(target, ToolOutcome):
            return target
        where = f" on workspace {target.value}" if target else ""

        existing = [
            window
            for window in snapshot.windows
            if window_matches(entry, (window.window_class, window.initial_class), ctx.learned(entry))
        ]
        # A terminal is opened fresh: "open kitty" means another terminal, never the one you're typing in.
        reuse = ctx.settings.apps.reuse_existing and not a.new_window and not _is_terminal(entry)
        if existing and reuse:
            window = snapshot.by_recency(existing)[0]
            if target and window.workspace.name != target.value:
                return Prepared(
                    f"Move the open {entry.name} window to workspace {target.value}",
                    _LaunchPlan(entry, target, "move-existing", window=window),
                )
            if target:
                return Prepared(
                    f"{entry.name} is already open on workspace {target.value}",
                    _LaunchPlan(entry, target, "already-there", window=window),
                )
            return Prepared(
                f"Switch to the open {entry.name} window",
                _LaunchPlan(entry, None, "focus-existing", window=window),
            )

        argv = ctx.launch_argv(entry)
        if argv is None:
            return ToolOutcome(
                StepStatus.FAILED, f"{entry.name} runs in a terminal, and no supported terminal was found"
            )
        return Prepared(f"Open {entry.name}{where}", _LaunchPlan(entry, target, "launch", argv=argv))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        plan = cast(_LaunchPlan, prepared.payload)
        if plan.mode == "launch":
            return await self._launch(plan, ctx)
        window = _require(plan.window, "existing window")
        ref = WindowRef.of(window)
        name = plan.entry.name
        timeout = ctx.settings.verify.timeout_s
        if plan.mode == "already-there":
            target = _require(plan.target, "target workspace")
            snapshot = await ctx.desktop.snapshot()
            check = window_on_workspace(snapshot, ref, target.value, name)
            return outcome_from_checks(
                [check],
                f"{name} was already on workspace {target.value}",
                f"{name} is no longer where it was",
            )
        if plan.mode == "move-existing":
            target = _require(plan.target, "target workspace")
            moved = await _move_and_verify(ctx, window, target, name)
            if isinstance(moved, ToolOutcome):
                return moved
            return outcome_from_checks(
                moved,
                f"Moved the open {name} window to workspace {target.value}",
                f"{name} did not end up on workspace {target.value}",
            )
        try:
            await ctx.desktop.focus(window)
        except HyprlandError as exc:
            return _failed_dispatch(f"focus {name}", exc)
        _, snapshot = await ctx.desktop.wait_for(
            lambda s: window_focused(s, ref, name).passed or None, timeout_s=timeout
        )
        check = window_focused(snapshot, ref, name)
        return outcome_from_checks([check], f"Switched to the open {name} window", f"Could not focus {name}")

    async def _launch(self, plan: _LaunchPlan, ctx: ToolContext) -> ToolOutcome:
        entry = plan.entry
        before = await ctx.desktop.snapshot()
        try:
            await ctx.desktop.launch(plan.argv, plan.target)
        except HyprlandError as exc:
            return _failed_dispatch(f"start {entry.name}", exc)

        learned = ctx.learned(entry)

        def new_app_window(snapshot: WorldSnapshot) -> Window | None:
            for window in snapshot.new_windows(before):
                if window_matches(entry, (window.window_class, window.initial_class), learned):
                    return window
            return None

        timeout = ctx.settings.apps.launch_timeout_s
        window, snapshot = await ctx.desktop.wait_for(new_app_window, timeout_s=timeout)
        if window is None:
            others = snapshot.new_windows(before)
            if others:
                classes = ", ".join(sorted({w.window_class for w in others}))
                return ToolOutcome(
                    StepStatus.UNVERIFIED,
                    f"Started {entry.name}, but the new window's class is '{classes}', "
                    f"so I can't confirm it's {entry.name}",
                    data={"new_window_classes": classes},
                )
            return ToolOutcome(
                StepStatus.FAILED,
                f"{entry.name} did not open a window within {timeout:.0f}s "
                "(it may still be starting, or it handed off to an already-running instance)",
            )

        ref = WindowRef.of(window)
        if plan.target and window.workspace.name != plan.target.value:
            # Recovery: single-instance apps ignore launch rules — move the window ourselves.
            opened_on = window.workspace.name
            moved = await _move_and_verify(ctx, window, plan.target, entry.name)
            if isinstance(moved, ToolOutcome):
                return moved
            return outcome_from_checks(
                [window_exists(snapshot, ref, entry.name), *moved],
                f"Opened {entry.name} on workspace {plan.target.value} "
                f"(it appeared on {opened_on}; moved it)",
                f"Opened {entry.name} on workspace {opened_on}, but could not move it to {plan.target.value}",
            )
        checks = [window_exists(snapshot, ref, entry.name)]
        if plan.target:
            checks.append(window_on_workspace(snapshot, ref, plan.target.value, entry.name))
        where = (
            f" on workspace {plan.target.value}" if plan.target else f" on workspace {window.workspace.name}"
        )
        return outcome_from_checks(
            checks, f"Opened {entry.name}{where}", f"{entry.name} opened but not where expected"
        )


async def _move_and_verify(
    ctx: ToolContext, window: Window, target: WorkspaceTarget, label: str
) -> list[Check] | ToolOutcome:
    """Move a window, wait for it to arrive, and return the check (or a failed-dispatch outcome)."""
    ref = WindowRef.of(window)
    try:
        await ctx.desktop.move(window, target)
    except HyprlandError as exc:
        return _failed_dispatch(f"move {label}", exc)
    _, snapshot = await ctx.desktop.wait_for(
        lambda s: window_on_workspace(s, ref, target.value, label).passed or None,
        timeout_s=ctx.settings.verify.timeout_s,
    )
    return [window_on_workspace(snapshot, ref, target.value, label)]


# ---------------------------------------------------------------------------- windows.move
class MoveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WindowTarget
    workspace: WorkspaceArg = Field(description="workspace number or name")
    all: bool = Field(default=False, description="true only if the user said all of the matching windows")


class MoveWindow(Tool):
    name = "windows.move"
    description = (
        "Move an open window to another workspace ('move/put/send X to N'); the user stays where they are."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.WINDOW_MOVE})
    Args = MoveArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(MoveArgs, args)
        target = _workspace(a.workspace)
        if isinstance(target, ToolOutcome):
            return target
        target = _require(target, "workspace")
        windows = _target_windows(
            a.window, a.all, ctx, snapshot, verb="move", choice=choice, already_at=target.value
        )
        if isinstance(windows, ToolOutcome):
            return windows
        labels = ", ".join(window_label(w) for w in windows)
        return Prepared(f"Move {labels} to workspace {target.value}", (windows, target))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        windows, target = cast(tuple[tuple[Window, ...], WorkspaceTarget], prepared.payload)
        checks: list[Check] = []
        for window in windows:
            moved = await _move_and_verify(ctx, window, target, window.window_class)
            if isinstance(moved, ToolOutcome):
                return moved
            checks.extend(moved)
        names = ", ".join(sorted({w.window_class for w in windows}))
        return outcome_from_checks(
            checks,
            f"Moved {names} to workspace {target.value}",
            f"Could not move {names} to workspace {target.value}",
        )


# ---------------------------------------------------------------------------- windows.focus
class FocusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WindowTarget


class FocusWindow(Tool):
    name = "windows.focus"
    description = "Go to an open window: shows its workspace and focuses it ('take me to X', 'switch to X')."
    risk = Risk.LOW
    effects = frozenset({Effect.WINDOW_FOCUS, Effect.WORKSPACE_SHOW})
    Args = FocusArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(FocusArgs, args)
        match = find_windows(a.window, snapshot, ctx)
        if match.problem:
            return ToolOutcome(StepStatus.FAILED, match.problem.capitalize())
        # Prefer the window the user's words point at; otherwise the most recently used match.
        window = narrow_by_request(match.windows, ctx.request_text, a.window)[0]
        return Prepared(f"Focus {window_label(window)}", window)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        window = cast(Window, prepared.payload)
        ref = WindowRef.of(window)
        try:
            await ctx.desktop.focus(window)
        except HyprlandError as exc:
            return _failed_dispatch(f"focus {window.window_class}", exc)
        _, snapshot = await ctx.desktop.wait_for(
            lambda s: window_focused(s, ref, window.window_class).passed or None,
            timeout_s=ctx.settings.verify.timeout_s,
        )
        check = window_focused(snapshot, ref, window.window_class)
        return outcome_from_checks(
            [check], f"Focused {window.window_class}", f"Could not focus {window.window_class}"
        )


# ---------------------------------------------------------------------------- workspaces.show
class ShowWorkspaceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: WorkspaceArg = Field(description="workspace number or name")


class ShowWorkspace(Tool):
    name = "workspaces.show"
    description = "Switch the screen to a workspace."
    risk = Risk.LOW
    effects = frozenset({Effect.WORKSPACE_SHOW})
    Args = ShowWorkspaceArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        target = _workspace(cast(ShowWorkspaceArgs, args).workspace)
        if isinstance(target, ToolOutcome):
            return target
        target = _require(target, "workspace")
        return Prepared(f"Show workspace {target.value}", target)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        target = cast(WorkspaceTarget, prepared.payload)
        try:
            await ctx.desktop.show_workspace(target)
        except HyprlandError as exc:
            return _failed_dispatch(f"show workspace {target.value}", exc)
        _, snapshot = await ctx.desktop.wait_for(
            lambda s: workspace_active(s, target.value).passed or None,
            timeout_s=ctx.settings.verify.timeout_s,
        )
        check = workspace_active(snapshot, target.value)
        return outcome_from_checks(
            [check], f"Showing workspace {target.value}", f"Workspace {target.value} is not shown"
        )


# ---------------------------------------------------------------------------- windows.close
class CloseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WindowTarget
    all: bool = Field(default=False, description="true only if the user said all of the matching windows")


class CloseWindow(Tool):
    name = "windows.close"
    description = "Politely close an open window (like clicking its close button; never force-kills)."
    risk = Risk.MEDIUM
    effects = frozenset({Effect.WINDOW_CLOSE})
    Args = CloseArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(CloseArgs, args)
        windows = _target_windows(a.window, a.all, ctx, snapshot, verb="close", choice=choice)
        if isinstance(windows, ToolOutcome):
            return windows
        return Prepared("Close " + ", ".join(window_label(w) for w in windows), windows)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        windows = cast(tuple[Window, ...], prepared.payload)
        refs = [(WindowRef.of(w), w.window_class) for w in windows]
        for window in windows:
            try:
                await ctx.desktop.close(window)
            except HyprlandError as exc:
                return _failed_dispatch(f"close {window.window_class}", exc)
        _, snapshot = await ctx.desktop.wait_for(
            lambda s: all(window_gone(s, ref, label).passed for ref, label in refs) or None,
            timeout_s=ctx.settings.verify.timeout_s,
        )
        checks = [window_gone(snapshot, ref, label) for ref, label in refs]
        names = ", ".join(sorted({label for _, label in refs}))
        return outcome_from_checks(
            checks,
            f"Closed {names}",
            f"{names} is still open — it may be asking to save changes (SONU never force-kills windows)",
        )


# ---------------------------------------------------------------------------- desktop.describe
class DescribeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DescribeDesktop(Tool):
    name = "desktop.describe"
    description = "Tell the user what is open on which workspace (read-only)."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.DESKTOP_READ})
    Args = DescribeArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        return Prepared("Describe the open windows", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        snapshot = await ctx.desktop.snapshot()
        return ToolOutcome(StepStatus.DONE, snapshot.describe(include_titles=True), private=True)


DEFAULT_TOOLS: tuple[Tool, ...] = (
    LaunchApp(),
    MoveWindow(),
    FocusWindow(),
    ShowWorkspace(),
    CloseWindow(),
    DescribeDesktop(),
)
