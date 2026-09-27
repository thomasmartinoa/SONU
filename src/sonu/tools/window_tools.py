"""Window states and arrangements: fullscreen, maximized, floating, pinned, side by side.

Hyprland 0.56.2's rules shape these (``src/config/shared/actions/ConfigActions.cpp``):

* only a floating window that isn't fullscreen can be pinned, so pinning floats it first;
* a pinned window shows on every workspace — it's tiled again only after unpinning;
* dispatchers answer "ok" even when nothing changed, so every change is read back from
  ``j/clients`` (``fullscreen``: 0 none, 1 maximized, 2 fullscreen; ``floating``; ``pinned``).

"Side by side" moves both windows onto one workspace, tiled, and lets the tiler split it; it's
done only when their positions really are next to each other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.hyprland.dialect import WorkspaceTarget
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome, outcome_from_checks
from sonu.tools.context import ToolContext
from sonu.tools.desktop_tools import failed_dispatch, target_windows
from sonu.tools.targets import WindowTarget, find_windows, narrow_by_request, window_label
from sonu.verify.checks import Check
from sonu.world.snapshot import WindowRef, WorldSnapshot

WindowState = Literal["fullscreen", "maximized", "normal", "floating", "tiled", "pinned", "unpinned"]

FULLSCREEN = 2
MAXIMIZED = 1

_WANTED: dict[WindowState, Callable[[Window], bool]] = {
    "fullscreen": lambda w: w.fullscreen == FULLSCREEN,
    "maximized": lambda w: w.fullscreen == MAXIMIZED,
    "normal": lambda w: w.fullscreen == 0,
    "floating": lambda w: w.floating,
    "tiled": lambda w: not w.floating and not w.pinned,
    "pinned": lambda w: w.pinned,
    "unpinned": lambda w: not w.pinned,
}
_DONE: dict[WindowState, str] = {
    "fullscreen": "Made {} fullscreen",
    "maximized": "Maximized {}",
    "normal": "Put {} back to normal size",
    "floating": "Made {} float",
    "tiled": "Tiled {}",
    "pinned": "Pinned {} (it floats and shows on every workspace)",
    "unpinned": "Unpinned {}",
}
_IS: dict[WindowState, str] = {
    "normal": "{} isn't fullscreen or maximized",
    "unpinned": "{} isn't pinned",
}


@dataclass(frozen=True, slots=True)
class _Change:
    """One dispatch, and how to tell from a fresh snapshot that it happened."""

    what: str
    act: Callable[[], Awaitable[None]]
    happened: Callable[[Window], bool]


def _changes(window: Window, state: WindowState, ctx: ToolContext) -> list[_Change]:
    """What it takes to get ``window`` into ``state``, in order — nothing if it's there already."""
    desktop = ctx.desktop
    unfullscreen = _Change(
        "leave fullscreen",
        lambda: desktop.set_fullscreen(
            window, "fullscreen" if window.fullscreen == FULLSCREEN else "maximized", False
        ),
        lambda w: w.fullscreen == 0,
    )
    changes: list[_Change] = []
    if state in ("fullscreen", "maximized"):
        mode: Literal["fullscreen", "maximized"] = state
        changes.append(
            _Change(f"make it {state}", lambda: desktop.set_fullscreen(window, mode, True), _WANTED[state])
        )
    elif state == "normal":
        if window.fullscreen:
            changes.append(unfullscreen)
    elif state == "floating":
        changes.append(_Change("float it", lambda: desktop.set_floating(window, True), lambda w: w.floating))
    elif state == "tiled":
        if window.pinned:
            changes.append(
                _Change("unpin it", lambda: desktop.set_pinned(window, False), lambda w: not w.pinned)
            )
        if window.floating:
            changes.append(
                _Change("tile it", lambda: desktop.set_floating(window, False), lambda w: not w.floating)
            )
    elif state == "pinned":
        if window.fullscreen:
            changes.append(unfullscreen)
        if not window.floating:
            changes.append(
                _Change("float it", lambda: desktop.set_floating(window, True), lambda w: w.floating)
            )
        changes.append(_Change("pin it", lambda: desktop.set_pinned(window, True), lambda w: w.pinned))
    elif window.pinned:  # unpinned
        changes.append(_Change("unpin it", lambda: desktop.set_pinned(window, False), lambda w: not w.pinned))
    return [change for change in changes if not change.happened(window)]


async def _apply(
    changes: list[_Change], ref: WindowRef, ctx: ToolContext
) -> tuple[Window | None, str | None]:
    """Run the changes one by one, each confirmed before the next; (window now, what failed)."""
    current: Window | None = None
    for change in changes:
        try:
            await change.act()
        except HyprlandError as exc:
            return current, f"Hyprland refused to {change.what}: {exc}"

        def done(snapshot: WorldSnapshot, change: _Change = change) -> Window | None:
            window = snapshot.find(ref)
            return window if window is not None and change.happened(window) else None

        current, _ = await ctx.desktop.wait_for(done, timeout_s=ctx.settings.verify.timeout_s)
        if current is None:
            return None, f"couldn't {change.what}"
    return current, None


# ---------------------------------------------------------------------------- windows.state
class StateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WindowTarget
    state: WindowState = Field(
        description="fullscreen, maximized, normal (leave fullscreen/maximized), floating, tiled, "
        "pinned (on every workspace), unpinned"
    )
    all: bool = Field(default=False, description="true only if the user said all of the matching windows")


class SetWindowState(Tool):
    name = "windows.state"
    description = (
        "Make a window fullscreen or maximized (or back to normal), floating or tiled, pinned to every "
        "workspace or unpinned."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.WINDOW_STATE})
    Args = StateArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(StateArgs, args)
        windows = target_windows(a.window, a.all, ctx, snapshot, verb=f"make {a.state}", choice=choice)
        if isinstance(windows, ToolOutcome):
            return windows
        labels = ", ".join(window_label(w) for w in windows)
        verb = {"normal": "Take {} out of fullscreen", "unpinned": "Unpin {}", "pinned": "Pin {}"}.get(
            a.state, f"Make {{}} {a.state}"
        )
        return Prepared(verb.format(labels), (windows, a.state))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        windows, state = cast(tuple[tuple[Window, ...], WindowState], prepared.payload)
        checks: list[Check] = []
        for window in windows:
            label = window.window_class
            ref = WindowRef.of(window)
            current, problem = await _apply(_changes(window, state, ctx), ref, ctx)
            if problem is not None and problem.startswith("Hyprland refused"):
                return ToolOutcome(StepStatus.FAILED, problem)
            now = (await ctx.desktop.snapshot()).find(ref) if current is None else current
            passed = now is not None and _WANTED[state](now)
            evidence = (
                {"fullscreen": now.fullscreen, "floating": now.floating, "pinned": now.pinned}
                if now is not None
                else {"found": False}
            )
            checks.append(Check(_IS.get(state, f"{{}} is {state}").format(label), passed, evidence))
        names = ", ".join(sorted({w.window_class for w in windows}))
        return outcome_from_checks(
            checks,
            _DONE[state].format(names),
            f"Couldn't make {names} {state}",
            windows=[w.address for w in windows],
        )


# ---------------------------------------------------------------------------- windows.side_by_side
class SideBySideArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: WindowTarget
    second: WindowTarget
    workspace: int | str | None = Field(
        default=None, description="only if the user named one; otherwise the first window's workspace"
    )


def _pick_two(
    first: WindowTarget, second: WindowTarget, ctx: ToolContext, snapshot: WorldSnapshot
) -> tuple[Window, Window] | ToolOutcome:
    """Two different windows: the ones the user's words point at, else the most recently used
    ("put my two terminals side by side" takes the two latest terminals)."""
    chosen: list[Window] = []
    for target in (first, second):
        match = find_windows(target, snapshot, ctx)
        if match.problem:
            return ToolOutcome(StepStatus.FAILED, match.problem.capitalize())
        ranked = narrow_by_request(match.windows, ctx.request_text, target, ignore=snapshot.origin_address)
        rest = [w for w in (*ranked, *match.windows) if w.address not in {c.address for c in chosen}]
        if not rest:
            return ToolOutcome(StepStatus.FAILED, f"There's only one {target.describe()} open")
        chosen.append(rest[0])
    return chosen[0], chosen[1]


def side_by_side(snapshot: WorldSnapshot, a: WindowRef, b: WindowRef, workspace: str) -> Check:
    """Both on the workspace, tiled, next to each other: their columns don't overlap, their rows do."""
    one, two = snapshot.find(a), snapshot.find(b)
    if one is None or two is None:
        return Check("both windows are side by side", False, {"found": [one is not None, two is not None]})
    left, right = sorted((one, two), key=lambda w: w.at[0])
    beside = left.at[0] + left.size[0] <= right.at[0]
    overlap = left.at[1] < right.at[1] + right.size[1] and right.at[1] < left.at[1] + left.size[1]
    placed = all(w.workspace.name == workspace and not w.floating and not w.fullscreen for w in (one, two))
    return Check(
        f"{one.window_class} and {two.window_class} are side by side on workspace {workspace}",
        placed and beside and overlap,
        {
            "left": [left.window_class, *left.at, *left.size, left.workspace.name],
            "right": [right.window_class, *right.at, *right.size, right.workspace.name],
        },
    )


class SideBySide(Tool):
    name = "windows.side_by_side"
    description = "Put two windows side by side on one workspace (both tiled), and show that workspace."
    risk = Risk.LOW
    effects = frozenset({Effect.WINDOW_STATE, Effect.WINDOW_MOVE, Effect.WORKSPACE_SHOW})
    Args = SideBySideArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(SideBySideArgs, args)
        pair = _pick_two(a.first, a.second, ctx, snapshot)
        if isinstance(pair, ToolOutcome):
            return pair
        try:
            target = WorkspaceTarget.of(a.workspace if a.workspace is not None else pair[0].workspace.name)
        except ValueError as exc:
            return ToolOutcome(StepStatus.FAILED, f"can't use workspace {a.workspace!r}: {exc}")
        labels = f"{window_label(pair[0])} and {window_label(pair[1])}"
        return Prepared(f"Put {labels} side by side on workspace {target.value}", (pair, target))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        pair, target = cast(tuple[tuple[Window, Window], WorkspaceTarget], prepared.payload)
        refs = (WindowRef.of(pair[0]), WindowRef.of(pair[1]))
        for window, ref in zip(pair, refs, strict=True):
            _, problem = await _apply(
                [*_changes(window, "normal", ctx), *_changes(window, "tiled", ctx)], ref, ctx
            )
            if problem is not None:
                return ToolOutcome(StepStatus.FAILED, f"{window.window_class}: {problem}")
            if window.workspace.name != target.value:
                try:
                    await ctx.desktop.move(window, target)
                except HyprlandError as exc:
                    return failed_dispatch(f"move {window.window_class}", exc)
        try:
            await ctx.desktop.show_workspace(target)
        except HyprlandError as exc:
            return failed_dispatch(f"show workspace {target.value}", exc)
        _, snapshot = await ctx.desktop.wait_for(
            lambda s: side_by_side(s, *refs, target.value).passed or None,
            timeout_s=ctx.settings.verify.timeout_s,
        )
        check = side_by_side(snapshot, *refs, target.value)
        others = [
            w
            for w in snapshot.windows
            if w.workspace.name == target.value
            and not w.floating
            and w.address not in {r.address for r in refs}
        ]
        names = f"{pair[0].window_class} and {pair[1].window_class}"
        failure = f"{names} are on workspace {target.value}, but not side by side"
        if others:
            failure += f" ({len(others)} other window{'s' if len(others) > 1 else ''} share it)"
        return outcome_from_checks(
            [check],
            f"Put {names} side by side on workspace {target.value}",
            failure,
            windows=[pair[0].address, pair[1].address],
        )


WINDOW_TOOLS: tuple[Tool, ...] = (SetWindowState(), SideBySide())
