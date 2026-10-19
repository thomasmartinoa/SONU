"""The developer operator (M4, D51): make a folder, open Claude Code there, type into it, switch its model,
see what it's doing.

SONU types only into terminals it opened itself (``sonu.integrations.kitty``) — never into whatever
window is focused. What it types goes to Claude Code, and from there to Anthropic: so it must be the
user's own words, and SONU shows the exact text and asks first (medium risk). Claude Code's own
question "do you trust this folder?" becomes SONU's question: if the folder isn't trusted yet, opening
Claude Code there asks first, and SONU answers Claude Code only after the user said yes.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.prompt_writer import PromptWriterError
from sonu.desktop.hyprland.dialect import WorkspaceTarget
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.integrations.claude_code import ClaudeScreen, compact, trusted
from sonu.integrations.kitty import Terminal
from sonu.system.commands import CommandError
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
from sonu.tools.file_tools import find_folders, home_relative, inside_home
from sonu.tools.grounding import ungrounded_words
from sonu.tools.targets import workspace_said
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

_CLAUDE = re.compile(r"\bclaude\b", re.I)
_FOLDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
START_TIMEOUT_S = 25.0  # Claude Code takes a few seconds to start
SCREEN_TIMEOUT_S = 5.0
MAX_TEXT = 4000


def _not_the_users(ctx: ToolContext, name: str, value: str | None) -> ToolOutcome | None:
    if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
        return ToolOutcome(
            StepStatus.FAILED, f"The {name} must be your own words ({', '.join(missing)} isn't)"
        )
    return None


def _where(ctx: ToolContext, path: Path) -> str:
    return home_relative(path, ctx.system.home)


def _names(folder: Path, place: str) -> bool:
    """Whether ``place`` names ``folder``: "shop-app", or its path ("dev_works/shop-app", "~/…", "/home/…").
    Found in `sonu eval --cloud`: the model named the folder this request made by its path."""
    wanted = [compact(part) for part in Path(place.strip().removeprefix("~")).parts if compact(part)]
    have = [compact(part) for part in folder.parts]
    return bool(wanted) and have[-len(wanted) :] == wanted


def _recent_projects(home: Path, limit: int = 5) -> list[Path]:
    """Folders with a git repository one or two levels below home (``~/x``, ``~/dev_works/x``), newest
    first."""
    found: list[Path] = []
    for pattern in ("*/.git", "*/*/.git"):
        found += [git.parent for git in home.glob(pattern) if not git.parent.name.startswith(".")]
    found = [
        path
        for path in dict.fromkeys(found)
        if not any(p.startswith(".") for p in path.relative_to(home).parts)
    ]
    found.sort(key=lambda path: (path / ".git").stat().st_mtime, reverse=True)
    return found[:limit]


def _pick_folder(ctx: ToolContext, place: str | None, choice: str | None) -> Path | ToolOutcome:
    """The folder the user means: their words, the folder this task made, or the one they picked."""
    home = ctx.system.home
    if choice:
        path = Path(choice)
        return (
            path
            if path.is_dir() and inside_home(path, home)
            else ToolOutcome(StepStatus.FAILED, "That folder is gone")
        )
    task = ctx.task_folder  # made by an earlier step of this request (in a dry run: would be)
    if task is not None and (not place or _names(task, place)):
        return task
    if not place:
        # Found on the laptop: "open a terminal and Claude Code in it" failed with "Which folder?". A question
        # with the likely answers instead: your recent projects, and your home folder.
        options = tuple(Choice(_where(ctx, path), str(path)) for path in [*_recent_projects(home), home])
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            "Which folder?",
            question="Open Claude Code in which folder?",
            choices=Choices("Open Claude Code in which folder?", options),
        )
    found = find_folders(place, home)
    if not found:
        return ToolOutcome(
            StepStatus.FAILED, f"I couldn't find a folder called '{place}' in your home folder"
        )
    if len(found) > 1:
        options = tuple(Choice(_where(ctx, path), str(path)) for path in found[:6])
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            f"{len(found)} folders match '{place}'",
            question=f"Which '{place}' folder?",
            choices=Choices(f"Which '{place}' folder?", options),
        )
    return found[0]


async def _terminal(ctx: ToolContext, which: str | None) -> Terminal | ToolOutcome:
    """The Claude Code SONU opened that is meant: one whose folder the user's words name, else the one
    used last. Only Claude Code: a command's window (dev.run) or an install's stays open afterwards."""
    open_ones = [t for t in await ctx.sonu_terminals().open_ones() if t.program == "claude"]
    if not open_ones:
        return ToolOutcome(
            StepStatus.FAILED,
            "I haven't opened Claude Code anywhere yet — say “open Claude Code in <folder>” first",
        )
    if which:
        wanted = {
            w
            for w in re.split(r"[^a-z0-9]+", which.lower())
            if w and w not in {"in", "the", "claude", "code"}
        }
        named = [t for t in open_ones if wanted & set(re.split(r"[^a-z0-9]+", Path(t.folder).name.lower()))]
        if named:
            return named[0]
    return open_ones[0]


async def _screen_until(
    ctx: ToolContext, terminal: Terminal, done: Callable[[ClaudeScreen], bool], timeout_s: float
) -> ClaudeScreen:
    """Read the terminal until ``done(screen)`` or the time runs out; the last screen read."""
    deadline = time.monotonic() + timeout_s
    screen = ClaudeScreen("")
    while True:
        try:
            screen = ClaudeScreen(await ctx.sonu_terminals().screen(terminal))
        except CommandError:
            screen = ClaudeScreen("")
        if done(screen) or time.monotonic() >= deadline:
            return screen
        await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------- files.make_folder
class MakeFolderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="the new folder's name, the user's words")
    inside: str | None = Field(
        default=None, max_length=120, description="the folder to make it in (user's words); none: home"
    )


class MakeFolder(Tool):
    name = "files.make_folder"
    description = "Make a new, empty folder (in the home folder, or inside a folder the user names)."
    risk = Risk.LOW
    effects = frozenset({Effect.FILE_WRITE})
    Args = MakeFolderArgs
    grounded = ("name", "inside")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(MakeFolderArgs, args)
        name = " ".join(a.name.split())
        for field, value in (("folder name", name), ("place", a.inside)):
            if problem := _not_the_users(ctx, field, value):
                return problem
        if not _FOLDER_NAME.match(name) or name in (".", ".."):
            return ToolOutcome(
                StepStatus.FAILED, f"'{name}' can't be a folder name (letters, digits, - _ . and spaces)"
            )
        parent = _pick_folder(ctx, a.inside, choice) if a.inside or choice else ctx.system.home
        if isinstance(parent, ToolOutcome):
            return parent
        target = parent / name
        if not inside_home(target, ctx.system.home):
            return ToolOutcome(StepStatus.FAILED, "SONU only makes folders inside your home folder")
        ctx.task_folder = target  # "…and open Claude Code there" (also in a dry run)
        return Prepared(f"Make the folder {_where(ctx, target)}", target)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        target = cast(Path, prepared.payload)
        existed = target.is_dir()
        try:
            target.mkdir(parents=False, exist_ok=True)
        except FileExistsError:
            return ToolOutcome(StepStatus.FAILED, f"{_where(ctx, target)} exists and isn't a folder")
        except OSError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't make {_where(ctx, target)}: {exc.strerror}")
        ctx.task_folder = target
        check = Check("the folder exists", target.is_dir(), {"folder": _where(ctx, target)})
        said = (
            f"{_where(ctx, target)} already existed" if existed else f"Made the folder {_where(ctx, target)}"
        )
        return outcome_from_checks([check], said, f"Couldn't make {_where(ctx, target)}")


# ---------------------------------------------------------------------------- dev.claude_open
class ClaudeOpenArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(
        default=None, max_length=120, description="the folder (user's words); none: the one this request made"
    )
    workspace: int | None = Field(default=None, ge=1, le=99)


class ClaudeOpen(Tool):
    name = "dev.claude_open"
    description = "Open Claude Code in a folder, in a new terminal SONU can type into."
    risk = Risk.LOW
    effects = frozenset({Effect.APP_LAUNCH})
    Args = ClaudeOpenArgs
    grounded = ("folder",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ClaudeOpenArgs, args)
        if problem := _not_the_users(ctx, "folder", a.folder):
            return problem
        if not ctx.which("kitty") or not ctx.which("claude"):
            missing = "kitty" if not ctx.which("kitty") else "Claude Code (claude)"
            return ToolOutcome(StepStatus.FAILED, f"{missing} isn't installed")
        folder = _pick_folder(ctx, a.folder, choice)
        if isinstance(folder, ToolOutcome):
            return folder
        workspace = a.workspace
        if workspace is None:
            # Found on real hardware: "…open claude code there on workspace 9 and tell it …" was planned
            # without the workspace. The user's words win (like D35's browser).
            workspace = workspace_said(ctx.request_text, _CLAUDE)
        target = WorkspaceTarget.of(workspace) if workspace else None
        where = f" on workspace {workspace}" if workspace else ""
        # A folder this request just made is new and empty: trusting it needs no question.
        if trusted(folder, ctx.system.home) or folder == ctx.task_folder:
            return Prepared(f"Open Claude Code in {_where(ctx, folder)}{where}", (folder, target, True))
        # Claude Code asks "do you trust this folder?" — the user answers through SONU's question.
        return Prepared(
            f"Open Claude Code in {_where(ctx, folder)}{where} and trust the folder "
            "(Claude Code will be able to read, change and run files in it)",
            (folder, target, True),
            risk=Risk.MEDIUM,
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        folder, target, trust = cast("tuple[Path, WorkspaceTarget | None, bool]", prepared.payload)
        terminals = ctx.sonu_terminals()
        terminal, argv = terminals.new(folder, "claude")
        before = await ctx.desktop.snapshot()
        try:
            await ctx.desktop.launch(argv, target)
        except HyprlandError as exc:
            terminals.forget(terminal)
            return ToolOutcome(StepStatus.FAILED, f"Couldn't start the terminal: {exc}")
        window, _ = await ctx.desktop.wait_for(
            lambda s: next((w for w in s.new_windows(before) if w.title == terminal.title), None),
            timeout_s=ctx.settings.apps.launch_timeout_s,
        )
        if window is None:
            terminals.forget(terminal)
            return ToolOutcome(StepStatus.FAILED, "The terminal for Claude Code didn't open")
        screen = await _screen_until(ctx, terminal, lambda s: s.ready or s.asks_trust, START_TIMEOUT_S)
        if screen.asks_trust and trust:
            await terminals.keys(terminal, "Down", "Enter")  # "Yes, I trust this folder"
            screen = await _screen_until(ctx, terminal, lambda s: s.ready, START_TIMEOUT_S)
        checks = [
            Check("the terminal is open", True, {"workspace": window.workspace.name}),
            Check("Claude Code is ready", screen.ready, {"trust_question": screen.asks_trust}),
        ]
        if target is not None:
            checks.append(Check(f"it's on workspace {target.value}", window.workspace.name == target.value))
        terminals.touch(terminal)
        where = f" on workspace {window.workspace.name}"
        if screen.asks_trust:
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Claude Code opened in {_where(ctx, folder)}{where}, but it's asking whether to trust "
                "the folder — answer it in the window",
                tuple(checks),
            )
        model = f" ({screen.model})" if screen.model else ""
        return outcome_from_checks(
            checks,
            f"Opened Claude Code{model} in {_where(ctx, folder)}{where}",
            f"Started Claude Code in {_where(ctx, folder)}, but it didn't come up ready",
            windows=[window.address],
        )


# ---------------------------------------------------------------------------- dev.type
class TypeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT, description="exactly what to type, the user's words")
    terminal: str | None = Field(
        default=None,
        max_length=80,
        description="which Claude Code, by its folder (user's words); none: the last one",
    )
    enter: bool = Field(default=True, description="press Enter after it (send it)")


class TypeInto(Tool):
    name = "dev.type"
    description = (
        "Type the user's words into the Claude Code (or other terminal) SONU opened, and send them. "
        "Only into terminals SONU opened."
    )
    risk = Risk.MEDIUM  # it goes to Claude Code (and from there to Anthropic): the exact text is shown first
    effects = frozenset({Effect.TERMINAL_INPUT})
    Args = TypeArgs
    grounded = ("text", "terminal")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(TypeArgs, args)
        for field, value in (("text", a.text), ("terminal", a.terminal)):
            if problem := _not_the_users(ctx, field, value):
                return problem
        terminal = await _terminal(ctx, a.terminal)
        if isinstance(terminal, ToolOutcome):
            return terminal
        shown = a.text if len(a.text) <= 300 else a.text[:297] + "…"
        send = "and send it" if a.enter else "(not sent yet)"
        return Prepared(f"Type into {terminal.name} {send}: “{shown}”", (terminal, a.text, a.enter))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        terminal, text, enter = cast("tuple[Terminal, str, bool]", prepared.payload)
        return await _send(ctx, terminal, text, enter=enter)


async def _send(
    ctx: ToolContext, terminal: Terminal, text: str, *, enter: bool = True, what: str = "it"
) -> ToolOutcome:
    """Type ``text`` into a SONU terminal and (``enter``) send it — each confirmed on its screen."""
    terminals = ctx.sonu_terminals()
    try:
        await terminals.type(terminal, text)
        typed = await _screen_until(ctx, terminal, lambda s: s.holds(text), SCREEN_TIMEOUT_S)
        if not typed.holds(text):
            return ToolOutcome(
                StepStatus.FAILED, f"Typed into {terminal.name}, but the text didn't show up there"
            )
        if enter:
            await terminals.keys(terminal, "Enter")
            sent = await _screen_until(ctx, terminal, lambda s: not s.holds(text), SCREEN_TIMEOUT_S)
    except CommandError as exc:
        return ToolOutcome(StepStatus.FAILED, f"Couldn't type into {terminal.name}: {exc}")
    terminals.touch(terminal)
    if not enter:
        return outcome_from_checks(
            [Check("the text is in the input box", True)], f"Typed {what} into {terminal.name} (not sent)", ""
        )
    return outcome_from_checks(
        [Check("it left the input box", not sent.holds(text))],
        f"Sent {what} to {terminal.name}",
        f"Typed {what} into {terminal.name}, but couldn't see it being sent",
    )


# ---------------------------------------------------------------------------- dev.claude_model
class ModelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["sonnet", "opus", "haiku"]
    terminal: str | None = Field(default=None, max_length=80, description="which Claude Code, by its folder")


class ClaudeModel(Tool):
    name = "dev.claude_model"
    description = "Switch the Claude Code SONU opened to another model (/model sonnet, opus or haiku)."
    risk = Risk.LOW
    effects = frozenset({Effect.TERMINAL_INPUT})
    Args = ModelArgs
    grounded = ("terminal",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ModelArgs, args)
        terminal = await _terminal(ctx, a.terminal)
        if isinstance(terminal, ToolOutcome):
            return terminal
        return Prepared(
            f"Switch {terminal.name} to {a.model.capitalize()} (/model {a.model})", (terminal, a.model)
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        terminal, model = cast("tuple[Terminal, str]", prepared.payload)
        terminals = ctx.sonu_terminals()
        try:
            before = len(ClaudeScreen(await terminals.screen(terminal)).model_set())
            # Claude Code's own menu, on purpose (see claude_code.py): "/model", Enter, the row, "s".
            await terminals.type(terminal, "/model")
            await _screen_until(ctx, terminal, lambda s: s.input_line() == "/model", SCREEN_TIMEOUT_S)
            await terminals.keys(terminal, "Enter")
            menu = await _screen_until(ctx, terminal, lambda s: s.model_menu, SCREEN_TIMEOUT_S)
            rows = menu.menu_rows()
            wanted = next((i for i, (label, _) in enumerate(rows) if label.lower().startswith(model)), None)
            current = next((i for i, (_, selected) in enumerate(rows) if selected), None)
            if not menu.model_menu or wanted is None or current is None:
                if menu.model_menu:
                    await terminals.keys(terminal, "Escape")
                return ToolOutcome(StepStatus.FAILED, f"{terminal.name} didn't offer {model.capitalize()}")
            steps = wanted - current
            if steps:
                await terminals.keys(terminal, *(["Down"] * steps if steps > 0 else ["Up"] * -steps))
            await _screen_until(
                ctx,
                terminal,
                lambda s: any(sel and label.lower().startswith(model) for label, sel in s.menu_rows()),
                2.0,
            )
            await terminals.keys(terminal, "s")  # this session only: the user's default stays
            screen = await _screen_until(
                ctx, terminal, lambda s: len(s.model_set()) > before, SCREEN_TIMEOUT_S
            )
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't switch {terminal.name}: {exc}")
        terminals.touch(terminal)
        now = screen.model_set()[-1] if len(screen.model_set()) > before else None
        switched = now is not None and now.lower().startswith(model)
        name = now.split(" for ")[0] if now else model
        return outcome_from_checks(
            [Check(f"Claude Code says it's on {model}", switched, {"model": now})],
            f"{terminal.name} is on {name} now (for this session)",
            f"Tried to switch {terminal.name} to {model.capitalize()}, but it didn't confirm the switch",
        )


# ---------------------------------------------------------------------------- dev.read
class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terminal: str | None = Field(default=None, max_length=80, description="which Claude Code, by its folder")


class ClaudeRead(Tool):
    name = "dev.read"
    description = (
        "What the Claude Code SONU opened is doing: working, waiting for the user, or asking something."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.DESKTOP_READ})
    Args = ReadArgs
    grounded = ("terminal",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        terminal = await _terminal(ctx, cast(ReadArgs, args).terminal)
        if isinstance(terminal, ToolOutcome):
            return terminal
        return Prepared(f"Look at {terminal.name}", terminal)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        terminal = cast(Terminal, prepared.payload)
        try:
            screen = ClaudeScreen(await ctx.sonu_terminals().screen(terminal))
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read {terminal.name}: {exc}")
        if screen.asks_trust:
            state = "is asking whether to trust its folder"
        elif question := screen.asks_permission:
            # Claude Code's own words: shown to the user, never given to SONU's model.
            return ToolOutcome(StepStatus.DONE, f"{terminal.name} is asking: {question}", private=True)
        elif screen.working:
            state = "is working"
        elif screen.ready:
            state = "is waiting for you"
        else:
            state = "is open"
        model = f" ({screen.model})" if screen.model else ""
        return ToolOutcome(
            StepStatus.DONE, f"{terminal.name}{model} {state}", (Check("read its screen", True),)
        )


# ---------------------------------------------------------------------------- dev.prompt
class PromptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idea: str = Field(min_length=3, max_length=1500, description="the user's idea, in their own words")
    terminal: str | None = Field(default=None, max_length=80, description="which Claude Code, by its folder")


class WritePrompt(Tool):
    name = "dev.prompt"
    description = (
        "Write a clear prompt for Claude Code from the user's idea, show it to them, and on their yes "
        "send it to the Claude Code SONU opened."
    )
    risk = Risk.MEDIUM  # SONU's own text goes to Anthropic: only after the user saw all of it and said yes
    effects = frozenset({Effect.TERMINAL_INPUT})
    Args = PromptArgs
    grounded = ("idea", "terminal")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(PromptArgs, args)
        for field, value in (("idea", a.idea), ("terminal", a.terminal)):
            if problem := _not_the_users(ctx, field, value):
                return problem
        if ctx.writer is None:
            return ToolOutcome(StepStatus.FAILED, "There's no model to write the prompt with")
        terminal = await _terminal(ctx, a.terminal)
        if isinstance(terminal, ToolOutcome):
            return terminal
        try:
            prompt = await ctx.writer.write(a.idea, Path(terminal.folder).name)
        except PromptWriterError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't write the prompt: {exc}")
        first = prompt.splitlines()[0][:120]
        return Prepared(
            f"Send the prompt I wrote from your idea to {terminal.name}: “{first}…”",
            (terminal, prompt),
            details=prompt,
            spoken=f"send the prompt I wrote to {terminal.name} — it's on your screen",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        terminal, prompt = cast("tuple[Terminal, str]", prepared.payload)
        return await _send(ctx, terminal, prompt, what="the prompt")


DEV_TOOLS: tuple[Tool, ...] = (
    MakeFolder(),
    ClaudeOpen(),
    TypeInto(),
    ClaudeModel(),
    ClaudeRead(),
    WritePrompt(),
)
