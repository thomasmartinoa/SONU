"""Working on a project (D65): open it in VS Code (at a file and line), run one of its own commands,
say whether that passed, and what changed in it (read-only git).

* The project is found by SONU from the user's words ("my SONU project" → ~/dev_works/SONU), never
  given as a path by the model; folders that look like projects win over other folders of that name.
* A command is one the project lists (``package.json`` scripts, ``justfile``, ``Makefile``) or one
  SONU can tell from its files (``uv run pytest``) — never a command line (``sonu.system.projects``).
  It runs the project's own code, so SONU shows the exact command and asks first; one that deploys,
  publishes or releases is high risk. It runs in a SONU window the user can watch; it's done when
  its exit code (written by ``run_task.py``, not read from the output) is 0.
* Git is read only, and its answer is private: file names and commit messages are shown and said to
  the user, never given to a model (a commit message can be anyone's words).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.apps.resolver import normalize
from sonu.desktop.hyprland.dialect import WorkspaceTarget
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.models import Window
from sonu.system.commands import CommandError
from sonu.system.git import Changed, Changes, changes, recent_commits
from sonu.system.projects import Task, is_project, matching_tasks, project_tasks
from sonu.tools.base import (
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
from sonu.tools.desktop_tools import failed_dispatch, resolve_app
from sonu.tools.dev_tools import _names
from sonu.tools.file_tools import find_folders, home_relative, inside_home
from sonu.tools.grounding import ungrounded_words
from sonu.tools.targets import workspace_said
from sonu.tools.web_tools import compact
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

# Editors that open a folder as a project and take ``--goto file:line``: VS Code and its forks.
VSCODE_LIKE = ("code", "codium", "code-oss", "code-insiders", "cursor", "windsurf")
_NOT_NAME = {"project", "projects", "repo", "repository", "codebase", "folder", "my", "the", "in"}
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".next",
}
MAX_SCANNED = 20_000
MAX_CHOICES = 6
RUN_TASK = Path(__file__).parents[1] / "integrations" / "run_task.py"
SYSTEM_PYTHON = "/usr/bin/python3"
RUN_WAIT_S = 30.0  # how long SONU waits for a command to finish before saying it's still running
POLL_S = 0.5
KEEP_RUNS = 10  # the records of older runs are removed
# Counts the usual test runners print (pytest, jest, vitest, cargo): only numbers and these words
# are taken from the output, nothing else of it.
_COUNTS = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|warnings?|tests?)\b")


def _where(ctx: ToolContext, path: Path) -> str:
    return home_relative(path, ctx.system.home)


def _own_words(ctx: ToolContext, name: str, value: str | None) -> ToolOutcome | None:
    if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
        return ToolOutcome(
            StepStatus.FAILED, f"The {name} must be your own words ({', '.join(missing)} isn't)"
        )
    return None


def _chosen(choice: str, inside: Path, *, file: bool = False) -> Path | ToolOutcome:
    """A folder or file the user picked from SONU's own list, if it's still there."""
    path = Path(choice)
    there = path.is_file() if file else path.is_dir()
    gone = "That file is gone" if file else "That folder is gone"
    return path if there and inside_home(path, inside) else ToolOutcome(StepStatus.FAILED, gone)


def _is_file(text: str | None) -> bool:
    return bool(text) and Path(str(text)).is_file()


async def _project(
    ctx: ToolContext, place: str | None, choice: str | None, noun: str = "project"
) -> Path | ToolOutcome:
    """The project the user means: their words, the folder this request made, or the folder of the
    Claude Code SONU opened last."""
    home = ctx.system.home
    if choice:
        return _chosen(choice, home)
    task = ctx.task_folder  # made by an earlier step of this request (in a dry run: would be)
    if task is not None and (not place or _names(task, place)):
        return task
    if not place:
        claude = [t for t in await ctx.sonu_terminals().open_ones() if t.program == "claude"]
        if claude:
            return Path(claude[0].folder)
        return ToolOutcome(StepStatus.FAILED, f"Which {noun}? Say its name (“… in SONU”)")
    words = " ".join(w for w in normalize(place).split() if w not in _NOT_NAME) or place
    found = find_folders(words, home)
    projects = [folder for folder in found if is_project(folder)] or found
    if not projects:
        return ToolOutcome(
            StepStatus.FAILED, f"I couldn't find a {noun} called '{words}' in your home folder"
        )
    if len(projects) > 1:
        options = tuple(Choice(_where(ctx, path), str(path)) for path in projects[:MAX_CHOICES])
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            f"{len(projects)} folders match '{words}'",
            question=f"Which '{words}'?",
            choices=Choices(f"Which '{words}'?", options),
        )
    return projects[0]


# ---------------------------------------------------------------------------- dev.open_project
def find_files(folder: Path, words: str) -> list[Path]:
    """Files in the project the user's words name: "planner.py", "planner", "core/planner.py" (the
    end of its path). The project's own files only: no hidden folders, dependencies or build output."""
    parts = [compact(part) for part in words.strip().strip("'\"").split("/") if compact(part)]
    if not parts:
        return []
    exact: list[Path] = []
    stems: list[Path] = []
    scanned = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        above = [compact(part) for part in Path(root).relative_to(folder).parts]
        if above[len(above) - len(parts) + 1 :] != parts[:-1] or len(above) < len(parts) - 1:
            scanned += len(files)
        else:
            for name in sorted(files):
                if compact(name) == parts[-1]:
                    exact.append(Path(root) / name)
                elif compact(Path(name).stem) == parts[-1]:
                    stems.append(Path(root) / name)
            scanned += len(files)
        if scanned > MAX_SCANNED:
            break
    return exact or stems


@dataclass(frozen=True, slots=True)
class _EditorPlan:
    entry: AppEntry
    argv: tuple[str, ...]
    folder: Path
    file: Path | None


class OpenProjectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(
        default=None, max_length=120, description="the project, in the user's words ('SONU', 'my shop app')"
    )
    file: str | None = Field(default=None, max_length=160, description="a file in it, the user's words")
    line: int | None = Field(default=None, ge=1, le=1_000_000)
    editor: str | None = Field(default=None, max_length=60, description="the editor the user named, if any")


class OpenProject(Tool):
    name = "dev.open_project"
    description = (
        "Open a project folder in VS Code (or Codium/Cursor), optionally at a file and line: "
        "'open my SONU project', 'open planner.py in SONU at line 40'."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH, Effect.FILE_READ})
    Args = OpenProjectArgs
    grounded = ("folder", "file", "editor")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(OpenProjectArgs, args)
        for field, value in (("project", a.folder), ("file", a.file), ("editor", a.editor)):
            if problem := _own_words(ctx, field, value):
                return problem
        editor = self._editor(a.editor, ctx, snapshot)
        if isinstance(editor, ToolOutcome):
            return editor
        launch = ctx.launch_argv(editor)
        if launch is None:
            return ToolOutcome(StepStatus.FAILED, f"{editor.name} can't be started")
        file_choice = choice if _is_file(choice) else None
        folder = await _project(ctx, a.folder, None if file_choice else choice)
        if isinstance(folder, ToolOutcome):
            return folder
        if a.line is not None and not a.file:
            return ToolOutcome(
                StepStatus.FAILED, f"Which file in {_where(ctx, folder)} should open at line {a.line}?"
            )
        file: Path | None = None
        if file_choice:
            picked = _chosen(file_choice, folder, file=True)
            if isinstance(picked, ToolOutcome):
                return picked
            file = picked
        elif a.file:
            files = find_files(folder, a.file)
            if not files:
                return ToolOutcome(
                    StepStatus.FAILED, f"There's no file called '{a.file}' in {_where(ctx, folder)}"
                )
            if len(files) > 1:
                options = tuple(Choice(str(f.relative_to(folder)), str(f)) for f in files[:MAX_CHOICES])
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT,
                    f"{len(files)} files match '{a.file}'",
                    question=f"Which '{a.file}'?",
                    choices=Choices(f"Which '{a.file}'?", options),
                )
            file = files[0]
        argv = (*launch, str(folder))
        label = f"{_where(ctx, folder)} in {editor.name}"
        if file is not None:
            argv += ("--goto", f"{file}:{a.line or 1}")
            at = f" at line {a.line}" if a.line else ""
            label = f"{file.relative_to(folder)}{at} ({_where(ctx, folder)}) in {editor.name}"
        return Prepared(f"Open {label}", _EditorPlan(editor, argv, folder, file))

    @staticmethod
    def _editor(named: str | None, ctx: ToolContext, snapshot: WorldSnapshot) -> AppEntry | ToolOutcome:
        """The editor the user named; else one with a window open; else VS Code, Codium, Code - OSS…"""
        if named:
            entry = resolve_app(named, ctx)
            if isinstance(entry, ToolOutcome) or entry.executable in VSCODE_LIKE:
                return entry
            return ToolOutcome(
                StepStatus.FAILED,
                "SONU opens projects in VS Code-like editors (VS Code, Codium, Cursor), "
                f"not {entry.name} yet",
            )
        editors = [entry for name in VSCODE_LIKE for entry in ctx.resolver.catalog.by_executable(name)]
        for entry in editors:
            learned = ctx.learned(entry)
            if any(
                window_matches(entry, (w.window_class, w.initial_class), learned) for w in snapshot.windows
            ):
                return entry
        if editors:
            return editors[0]
        return ToolOutcome(StepStatus.FAILED, "VS Code isn't installed (nor Codium or Cursor)")

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        plan = cast(_EditorPlan, prepared.payload)
        before = await ctx.desktop.snapshot()
        titles = {window.address: window.title for window in before.windows}
        learned = ctx.learned(plan.entry)
        markers = [compact(plan.folder.name)] + ([compact(plan.file.name)] if plan.file else [])
        try:
            await ctx.desktop.launch(plan.argv)
        except HyprlandError as exc:
            return failed_dispatch(f"start {plan.entry.name}", exc)

        def showing(snapshot: WorldSnapshot) -> Window | None:
            active = snapshot.active_window
            for window in snapshot.windows:
                if not window_matches(plan.entry, (window.window_class, window.initial_class), learned):
                    continue
                fresh = titles.get(window.address) != window.title or (
                    active and active.address == window.address
                )
                if fresh and all(marker in compact(window.title) for marker in markers):
                    return window
            return None

        window, _ = await ctx.desktop.wait_for(showing, timeout_s=ctx.settings.apps.launch_timeout_s)
        what = plan.file.name if plan.file else _where(ctx, plan.folder)
        check = Check(
            f"{plan.entry.name} shows {what}",
            window is not None,
            {"workspace": window.workspace.name} if window else {},
        )
        return outcome_from_checks(
            [check],
            f"Opened {what} in {plan.entry.name}",
            f"{plan.entry.name} didn't show {what}, so I can't confirm it opened",
            **({"windows": [window.address]} if window else {}),
        )


# ---------------------------------------------------------------------------- dev.run
def _counts(screen: str) -> str:
    """ "3 failed, 120 passed" from the end of a test run's output (numbers and fixed words only)."""
    tail = "\n".join(screen.strip().splitlines()[-15:])
    seen: dict[str, str] = {}
    for number, word in _COUNTS.findall(tail):
        seen[word.rstrip("s") if word.startswith(("error", "warning", "test")) else word] = number
    return ", ".join(f"{n} {w}" for w, n in seen.items())


@dataclass(frozen=True, slots=True)
class _Run:
    id: str
    command: str
    folder: str
    terminal: str  # the SONU terminal's id
    title: str
    socket: str
    started: float

    @property
    def name(self) -> str:
        return f"{self.command} in {Path(self.folder).name}"


def _runs(ctx: ToolContext) -> list[_Run]:
    folder = ctx.sonu_terminals().folder
    runs: list[_Run] = []
    for path in folder.glob("*.run") if folder.is_dir() else []:
        try:
            runs.append(_Run(**json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(runs, key=lambda run: run.started, reverse=True)


def _forget_old_runs(ctx: ToolContext) -> None:
    folder = ctx.sonu_terminals().folder
    for run in _runs(ctx)[KEEP_RUNS:]:
        for suffix in (".run", ".exit"):
            (folder / f"{run.id}{suffix}").unlink(missing_ok=True)


def _exit_code(ctx: ToolContext, run: _Run) -> int | None:
    try:
        return int((ctx.sonu_terminals().folder / f"{run.id}.exit").read_text().strip())
    except (OSError, ValueError):
        return None


async def _run_state(ctx: ToolContext, run: _Run) -> tuple[int | None, bool, str]:
    """(exit code or None, whether its window is open, the counts it printed)."""
    terminals = ctx.sonu_terminals()
    terminal = next((t for t in terminals.records() if t.id == run.terminal), None)
    screen = ""
    if terminal is not None:
        try:
            screen = await terminals.screen(terminal)
        except CommandError:
            terminal = None
    return _exit_code(ctx, run), terminal is not None, _counts(screen)


def _result(run: _Run, code: int | None, open_now: bool, counts: str) -> ToolOutcome:
    shown = f" ({counts})" if counts else ""
    if code == 0:
        return ToolOutcome(
            StepStatus.DONE, f"{run.name} passed{shown}", (Check("it ended with exit code 0", True),)
        )
    if code is not None:
        return ToolOutcome(
            StepStatus.FAILED,
            f"{run.name} failed (exit code {code}{', ' + counts if counts else ''}) — the output is in "
            "its window",
            (Check("it ended with exit code 0", False, {"exit_code": code}),),
        )
    if open_now:
        return ToolOutcome(
            StepStatus.UNVERIFIED,
            f"{run.name} is still running in its window{shown} — you'll get a notification when it ends",
        )
    return ToolOutcome(StepStatus.FAILED, f"{run.name}: its window was closed before it finished")


class RunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(
        min_length=1, max_length=60, description="which command, the user's words ('tests', 'build')"
    )
    folder: str | None = Field(default=None, max_length=120, description="the project, the user's words")


class RunTask(Tool):
    name = "dev.run"
    description = (
        "Run one of a project's own commands (its tests, build, lint — from package.json, justfile, "
        "Makefile or pyproject) in a window the user can watch, and say whether it passed."
    )
    risk = Risk.MEDIUM  # the project's own code runs: the exact command is shown and asked first
    effects = frozenset({Effect.APP_LAUNCH, Effect.RUN_COMMAND})
    Args = RunArgs
    grounded = ("task", "folder")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(RunArgs, args)
        for field, value in (("command", a.task), ("project", a.folder)):
            if problem := _own_words(ctx, field, value):
                return problem
        if not ctx.which("kitty"):
            return ToolOutcome(
                StepStatus.FAILED, "kitty isn't installed (SONU runs commands in a kitty window)"
            )
        task_choice = choice if choice and choice.startswith("task:") else None
        folder = await _project(ctx, a.folder, None if task_choice else choice)
        if isinstance(folder, ToolOutcome):
            return folder
        where = _where(ctx, folder)
        tasks = project_tasks(folder)
        if not tasks:
            return ToolOutcome(
                StepStatus.FAILED,
                f"{where} lists no commands SONU can run (package.json scripts, a justfile or a Makefile)",
            )
        found = matching_tasks(tasks, a.task, ctx.which)
        if task_choice:
            found = [t for t in tasks if f"task:{t.source}:{t.name}" == task_choice]
        if not found:
            names = ", ".join(dict.fromkeys(t.name for t in tasks[:12]))
            return ToolOutcome(StepStatus.FAILED, f"{where} has no '{a.task}' command — it has: {names}")
        if len(found) > 1:
            options = tuple(
                Choice(f"{t.command} ({t.source})", f"task:{t.source}:{t.name}") for t in found[:MAX_CHOICES]
            )
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"{len(found)} commands in {where} match '{a.task}'",
                question=f"Which one: {', '.join(o.label for o in options)}?",
                choices=Choices(f"Which '{a.task}' command?", options),
            )
        task = found[0]
        if not ctx.which(task.argv[0]):
            return ToolOutcome(
                StepStatus.FAILED, f"{task.argv[0]} isn't installed (needed for {task.command})"
            )
        runs = f" — it runs “{task.runs}”" if task.runs and task.runs != task.command else ""
        outward = " It looks like it sends something off this computer." if task.outward else ""
        return Prepared(
            f"Run {task.command} in {where} (from {task.source}){runs}, in a window you can watch. "
            f"It runs the project's own code.{outward}",
            (folder, task),
            risk=Risk.HIGH if task.outward else None,
            spoken=f"run {task.command} in {folder.name}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        folder, task = cast("tuple[Path, Task]", prepared.payload)
        terminals = ctx.sonu_terminals()
        run_id = secrets.token_hex(4)
        status = terminals.folder / f"{run_id}.exit"
        terminal, launch = terminals.new(
            folder,
            SYSTEM_PYTHON,
            (str(RUN_TASK), str(status), f"--notify={task.command} in {folder.name}", *task.argv),
            label=f"{task.command} · {folder.name}",
            hold=True,  # the output stays to read
        )
        run = _Run(
            run_id, task.command, str(folder), terminal.id, terminal.title, terminal.socket, time.time()
        )
        record = terminals.folder / f"{run_id}.run"
        record.write_text(json.dumps(asdict(run)))
        record.chmod(0o600)
        _forget_old_runs(ctx)
        before = await ctx.desktop.snapshot()
        try:
            await ctx.desktop.launch(launch, None)
        except HyprlandError as exc:
            terminals.forget(terminal)
            record.unlink(missing_ok=True)
            return failed_dispatch("open the window", exc)
        window, _ = await ctx.desktop.wait_for(
            lambda s: next((w for w in s.new_windows(before) if w.title == terminal.title), None),
            timeout_s=ctx.settings.apps.launch_timeout_s,
        )
        if window is None:
            terminals.forget(terminal)
            record.unlink(missing_ok=True)
            return ToolOutcome(StepStatus.FAILED, f"The window for {task.command} didn't open")
        deadline = time.monotonic() + RUN_WAIT_S
        while _exit_code(ctx, run) is None and time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            if not await terminals.alive(terminal) and _exit_code(ctx, run) is None:
                break
        code, open_now, counts = await _run_state(ctx, run)
        outcome = _result(run, code, open_now, counts)
        opened = Check("its window opened", True, {"workspace": window.workspace.name})
        return ToolOutcome(
            outcome.status, outcome.summary, (opened, *outcome.checks), data={"windows": [window.address]}
        )


# ---------------------------------------------------------------------------- dev.run_result
class RunResultArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(default=None, max_length=120, description="the project, the user's words")


class RunResult(Tool):
    name = "dev.run_result"
    description = "Whether the last command SONU ran (dev.run) passed, failed or is still running."
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.DESKTOP_READ})
    Args = RunResultArgs
    grounded = ("folder",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        place = cast(RunResultArgs, args).folder
        if problem := _own_words(ctx, "project", place):
            return problem
        runs = _runs(ctx)
        if place:
            wanted = compact(" ".join(w for w in normalize(place).split() if w not in _NOT_NAME))
            runs = [run for run in runs if compact(Path(run.folder).name) == wanted]
        if not runs:
            return ToolOutcome(
                StepStatus.FAILED,
                "I haven't run anything there yet" if place else "I haven't run anything yet",
            )
        return Prepared(f"Look at {runs[0].name}", runs[0])

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        run = cast(_Run, prepared.payload)
        outcome = _result(run, *await _run_state(ctx, run))
        if outcome.status is StepStatus.FAILED and not outcome.checks:
            return outcome
        # Reading it worked: the answer is DONE even when the command itself failed.
        return ToolOutcome(StepStatus.DONE, outcome.summary, (Check("read how it ended", True),))


# ---------------------------------------------------------------------------- dev.git
def _file_line(changed: Changed) -> str:
    numbers = f" +{changed.added} -{changed.removed}" if changed.added is not None else ""
    kind = "" if changed.kind == "changed" else f" ({changed.kind})"
    return f"{changed.path}{kind}{numbers}"


def describe_changes(name: str, found: Changes, limit: int = 6) -> str:
    branch = f"on {found.branch}" if found.branch else "not on a branch"
    sync = []
    if found.ahead:
        sync.append(f"{found.ahead} commit{'s' if found.ahead > 1 else ''} not pushed")
    if found.behind:
        sync.append(f"{found.behind} behind {found.upstream}")
    where = f"{name} ({branch}{', ' + ', '.join(sync) if sync else ''})"
    edited, untracked = found.edited, found.untracked
    if not edited and not untracked:
        return f"{where}: no uncommitted changes"
    parts = []
    if edited:
        added = sum(f.added or 0 for f in edited)
        removed = sum(f.removed or 0 for f in edited)
        files = "; ".join(_file_line(f) for f in edited[:limit])
        more = f"; and {len(edited) - limit} more" if len(edited) > limit else ""
        parts.append(
            f"{len(edited)} file{'s' if len(edited) > 1 else ''} changed, +{added} -{removed}: {files}{more}"
        )
    if untracked:
        names = ", ".join(f.path for f in untracked[:3])
        more = f" and {len(untracked) - 3} more" if len(untracked) > 3 else ""
        parts.append(f"{len(untracked)} new untracked: {names}{more}")
    return f"{where}: " + ". ".join(parts)


class GitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(default=None, max_length=120, description="the project, the user's words")
    what: Literal["changes", "commits"] = Field(
        default="changes", description="changes: what's changed and not committed; commits: the last commits"
    )


class GitInfo(Tool):
    name = "dev.git"
    description = (
        "Read-only git for a project: what changed and isn't committed yet, or the last commits "
        "('what did I change in SONU?', 'what were my last commits in shop-app?')."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.FILE_READ})
    Args = GitArgs
    grounded = ("folder",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(GitArgs, args)
        if problem := _own_words(ctx, "project", a.folder):
            return problem
        folder = await _project(ctx, a.folder, choice)
        if isinstance(folder, ToolOutcome):
            return folder
        what = "what changed" if a.what == "changes" else "the last commits"
        return Prepared(f"Look at {what} in {_where(ctx, folder)} (git, read only)", (folder, a.what))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        folder, what = cast("tuple[Path, str]", prepared.payload)
        runner = ctx.system.runner
        try:
            if what == "changes":
                text = describe_changes(folder.name, await changes(runner, folder))
            else:
                commits = await recent_commits(runner, folder)
                text = (
                    f"The last commits in {folder.name}: "
                    + "; ".join(f"{c.short} {c.subject} ({c.when})" for c in commits)
                    if commits
                    else f"{folder.name} has no commits yet"
                )
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, str(exc))
        # File names and commit messages: shown and said to the user, never given to a model.
        return ToolOutcome(StepStatus.DONE, text, (Check("git answered", True),), private=True)


# ---------------------------------------------------------------------------- dev.terminal
# How each terminal is told the folder to start in (its shell starts there).
TERMINAL_FOLDER: dict[str, Callable[[str], tuple[str, ...]]] = {
    "kitty": lambda folder: ("--directory", folder),
    "alacritty": lambda folder: ("--working-directory", folder),
    "foot": lambda folder: (f"--working-directory={folder}",),
    "wezterm": lambda folder: ("start", "--cwd", folder),
    "ghostty": lambda folder: (f"--working-directory={folder}",),
    "konsole": lambda folder: ("--workdir", folder),
    "gnome-terminal": lambda folder: (f"--working-directory={folder}",),
}
SHELL_WAIT_S = 3.0  # the shell starts a moment after the window maps


def terminal_argv(entry: AppEntry, folder: Path) -> tuple[str, ...] | None:
    """The argv that starts ``entry`` (a terminal) in ``folder``; None when SONU can't tell it a folder."""
    start_in = TERMINAL_FOLDER.get(entry.executable)
    return (entry.argv[0], *start_in(str(folder))) if start_in and entry.argv else None


async def shell_started_in(ctx: ToolContext, pid: int, folder: Path) -> bool:
    """Whether a terminal's shell (a child of its process) is in ``folder`` — waiting for it to start."""
    deadline = time.monotonic() + SHELL_WAIT_S
    while folder not in ctx.system.child_folders(pid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)
    return True


@dataclass(frozen=True, slots=True)
class _TerminalPlan:
    entry: AppEntry
    argv: tuple[str, ...]
    folder: Path
    workspace: WorkspaceTarget | None


class TerminalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(
        default=None,
        max_length=120,
        description="the folder, the user's words; none: the one this request made",
    )
    app: str | None = Field(default=None, max_length=60, description="the terminal the user named, if any")
    workspace: int | None = Field(default=None, ge=1, le=99)


class OpenTerminal(Tool):
    name = "dev.terminal"
    description = (
        "Open the user's terminal in a folder ('open a terminal in dev_works', 'open kitty in SONU'): a new "
        "window whose shell starts there. Only when a folder is named or made in this request."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH})
    Args = TerminalArgs
    grounded = ("folder", "app")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(TerminalArgs, args)
        for field, value in (("folder", a.folder), ("terminal", a.app)):
            if problem := _own_words(ctx, field, value):
                return problem
        entry = resolve_app(a.app or "terminal", ctx)
        if isinstance(entry, ToolOutcome):
            return entry
        if entry.executable not in TERMINAL_FOLDER:
            return ToolOutcome(StepStatus.FAILED, f"SONU can't tell {entry.name} which folder to start in")
        folder = await _project(ctx, a.folder, choice, noun="folder")
        if isinstance(folder, ToolOutcome):
            return folder
        argv = terminal_argv(entry, folder)
        if argv is None:
            return ToolOutcome(StepStatus.FAILED, f"{entry.name} can't be started")
        workspace = a.workspace
        if workspace is None:  # like dev.claude_open: the workspace the user's words give it
            names = {entry.name, entry.executable, "terminal"}
            about = re.compile("|".join(rf"\b{re.escape(name)}\b" for name in names if name), re.I)
            workspace = workspace_said(ctx.request_text, about)
        where = f" on workspace {workspace}" if workspace else ""
        target = WorkspaceTarget.of(workspace) if workspace else None
        return Prepared(
            f"Open {entry.name} in {_where(ctx, folder)}{where}", _TerminalPlan(entry, argv, folder, target)
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        plan = cast(_TerminalPlan, prepared.payload)
        before = await ctx.desktop.snapshot()
        learned = ctx.learned(plan.entry)
        try:
            await ctx.desktop.launch(plan.argv, plan.workspace)
        except HyprlandError as exc:
            return failed_dispatch(f"start {plan.entry.name}", exc)

        def opened(snapshot: WorldSnapshot) -> Window | None:
            new = snapshot.new_windows(before)
            return next(
                (w for w in new if window_matches(plan.entry, (w.window_class, w.initial_class), learned)),
                None,
            )

        window, _ = await ctx.desktop.wait_for(opened, timeout_s=ctx.settings.apps.launch_timeout_s)
        where = _where(ctx, plan.folder)
        if window is None:
            return ToolOutcome(StepStatus.FAILED, f"{plan.entry.name} didn't open a window")
        # Checked on the process: its shell's working folder (/proc), not the title (a shell sets that).
        shell = Check(f"its shell is in {where}", await shell_started_in(ctx, window.pid, plan.folder))
        placed = [Check(f"{plan.entry.name} opened", True, {"workspace": window.workspace.name})]
        if plan.workspace is not None:
            wanted = plan.workspace.value
            placed.append(Check(f"it's on workspace {wanted}", window.workspace.name == wanted))
        checks = [*placed, shell]
        if not shell.passed and all(c.passed for c in placed):
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Opened {plan.entry.name}, but I couldn't see its shell start in {where}",
                tuple(checks),
                data={"windows": [window.address]},
            )
        return outcome_from_checks(
            checks,
            f"Opened {plan.entry.name} in {where} on workspace {window.workspace.name}",
            f"Opened {plan.entry.name}, but not where it should be",
            windows=[window.address],
        )


PROJECT_TOOLS: tuple[Tool, ...] = (OpenProject(), RunTask(), RunResult(), GitInfo(), OpenTerminal())
