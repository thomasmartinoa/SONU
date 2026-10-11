"""The tool contract.

A tool is the *only* way SONU acts. Each one declares its risk and effects (which the policy
engine reasons about), validates its arguments with a pydantic model (unknown fields are
rejected), and works in two phases:

1. ``prepare`` resolves the request against a fresh snapshot (which app? which window?) without
   changing anything, and describes exactly what it would do — this is what the user confirms;
2. ``execute`` acts, then verifies the result on new snapshots and returns checks as evidence.

The engine, not the tool, has the final say on status: a side-effecting step without passing
checks can never be reported as done.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ValidationError

from sonu.verify.checks import Check

if TYPE_CHECKING:
    from sonu.tools.context import ToolContext
    from sonu.world.snapshot import WorldSnapshot


class Risk(StrEnum):
    READ_ONLY = "read-only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FORBIDDEN = "forbidden"

    @property
    def rank(self) -> int:
        return list(Risk).index(self)


class Effect(StrEnum):
    DESKTOP_READ = "desktop.read"
    APP_LAUNCH = "app.launch"
    WINDOW_MOVE = "window.move"
    WINDOW_FOCUS = "window.focus"
    WORKSPACE_SHOW = "workspace.show"
    WINDOW_CLOSE = "window.close"
    WEB_OPEN = "web.open"  # sends a URL to the user's browser
    SETTINGS_CHANGE = "settings.change"  # volume, brightness, radios, power mode
    MEDIA_CONTROL = "media.control"
    SYSTEM_READ = "system.read"
    SCHEDULE = "schedule"  # something happens later (a reminder)
    SCREEN_CAPTURE = "screen.capture"
    FILE_READ = "file.read"  # looks at the user's folders (names, dates, types; never contents)
    POWER = "power"  # suspend, reboot, shut down, log out
    WINDOW_STATE = "window.state"  # fullscreen, floating, pinned, tiled next to another window
    FILE_WRITE = "file.write"  # creates folders (never changes or deletes what exists)
    TERMINAL_INPUT = "terminal.input"  # types into a terminal SONU opened (e.g. a prompt for Claude Code)
    GITHUB_WRITE = "github.write"  # changes something on GitHub (merges a pull request)
    INSTALL = "install"  # installs software (root, through the system's own password prompt)
    UNINSTALL = "uninstall"  # removes software (root, through the system's own password prompt)
    UI_INPUT = "ui.input"  # presses a control in an app (accessibility; no mouse)
    OUTSIDE = "outside.tool"  # a tool of another program (an MCP server the user added, D63)
    RUN_COMMAND = "run.command"  # runs one of a project's own commands (its tests, build) in a SONU window


class StepStatus(StrEnum):
    DONE = "done"  # executed and every check passed
    UNVERIFIED = "unverified"  # executed, but the result could not be confirmed
    FAILED = "failed"
    NEEDS_INPUT = "needs-input"  # SONU must ask the user something first
    DENIED = "denied"  # blocked by policy or by the user
    SKIPPED = "skipped"  # dry run, or not attempted


@dataclass(frozen=True, slots=True)
class Choice:
    label: str  # shown to the user (may contain a window title)
    value: str  # what the tool understands: a desktop id, a window address, or ALL_CHOICE


ALL_CHOICE = "*all*"


@dataclass(frozen=True, slots=True)
class Choices:
    """Answers SONU can offer when a step is ambiguous; the user picks one and the step runs again."""

    prompt: str
    options: tuple[Choice, ...]

    def label_for(self, value: str) -> str | None:
        return next((option.label for option in self.options if option.value == value), None)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    status: StepStatus
    summary: str
    checks: tuple[Check, ...] = ()
    question: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # True when the summary contains personal content (e.g. window titles): kept out of the audit log.
    private: bool = False
    # With NEEDS_INPUT: the answers the user can pick from (built by SONU from the desktop, not the model).
    choices: Choices | None = None


@dataclass(frozen=True, slots=True)
class Prepared:
    """A step resolved against the desktop, ready to be confirmed and executed."""

    description: str  # what will happen, in plain words (no window titles)
    payload: Any = None
    # Raises the risk of this particular step above the tool's own (e.g. turning Wi-Fi *off*).
    risk: Risk | None = None
    # Long text the user must see before approving (a prompt SONU wrote), and a short form to say aloud.
    details: str | None = None
    spoken: str | None = None


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]  # shown to the planner
    risk: ClassVar[Risk]
    effects: ClassVar[frozenset[Effect]]
    Args: ClassVar[type[BaseModel]]
    # Text arguments that must be the user's own words (see grounding.py). The tool checks them
    # itself; the planner also checks them, so the model can correct itself instead of failing.
    grounded: ClassVar[tuple[str, ...]] = ()

    def parse_args(self, raw: Mapping[str, Any]) -> BaseModel:
        """The arguments, checked (unknown fields rejected); raises ``ValueError`` when they're wrong."""
        return self.Args.model_validate(raw)

    def args_json_schema(self) -> dict[str, Any]:
        """The arguments' JSON schema: what the planner's output is held to, and what MCP clients get."""
        return self.Args.model_json_schema()

    @abstractmethod
    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        """Resolve the step without changing anything; return an outcome to stop early.

        ``choice`` is the value of the option the user picked after this step asked (one of the
        ``Choices`` it returned); the engine only passes values the tool itself offered.
        """

    @abstractmethod
    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        """Act, then verify on fresh snapshots."""


def args_problem(exc: ValueError) -> str:
    """What was wrong with a step's arguments, in one line."""
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
    return str(exc)


def outcome_from_checks(checks: list[Check], success: str, failure: str, **data: Any) -> ToolOutcome:
    """DONE only if there is at least one check and all of them passed."""
    if checks and all(check.passed for check in checks):
        return ToolOutcome(StepStatus.DONE, success, tuple(checks), data=data)
    return ToolOutcome(StepStatus.FAILED, failure, tuple(checks), data=data)
