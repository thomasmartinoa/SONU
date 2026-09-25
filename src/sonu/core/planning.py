"""What a planner produces, and the interface every planner implements."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from sonu.tools.registry import ToolRegistry
    from sonu.world.snapshot import WorldSnapshot

PlanKind = Literal["steps", "ask", "reply", "cannot"]


class PlanningError(Exception):
    """The planner could not produce a usable plan."""


@dataclass(frozen=True, slots=True)
class PlannedStep:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Plan:
    kind: PlanKind
    message: str = ""
    steps: tuple[PlannedStep, ...] = ()
    model: str = ""
    duration_s: float = 0.0
    attempts: int = 1


class Planner(Protocol):
    async def plan(
        self,
        request: str,
        snapshot: WorldSnapshot,
        registry: ToolRegistry,
        *,
        app_labels: Mapping[str, str] | None = None,
    ) -> Plan: ...
