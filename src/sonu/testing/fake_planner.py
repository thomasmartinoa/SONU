"""A deterministic planner for tests: returns scripted plans instead of calling a model."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sonu.core.conversation import Turn
from sonu.core.planning import Plan, PlannedStep, PlanningError
from sonu.tools.registry import ToolRegistry
from sonu.world.snapshot import WorldSnapshot


def steps_plan(
    *steps: tuple[str, dict[str, Any]], message: str = "", unsupported: tuple[str, ...] = ()
) -> Plan:
    return Plan(
        "steps",
        message,
        tuple(PlannedStep(tool, args) for tool, args in steps),
        unsupported=unsupported,
        model="scripted",
    )


class ScriptedPlanner:
    def __init__(self, plans: Mapping[str, Plan] | Callable[[str, WorldSnapshot], Plan]) -> None:
        self._plans = plans
        self.seen: list[tuple[str, WorldSnapshot]] = []
        self.histories: list[list[Turn]] = []  # the conversation each request was planned with

    async def plan(
        self,
        request: str,
        snapshot: WorldSnapshot,
        registry: ToolRegistry,
        *,
        app_labels: Mapping[str, str] | None = None,
        history: Sequence[Turn] = (),
        notes: Sequence[str] = (),
    ) -> Plan:
        self.seen.append((request, snapshot))
        self.histories.append(list(history))
        if callable(self._plans):
            return self._plans(request, snapshot)
        try:
            return self._plans[request]
        except KeyError as exc:
            raise PlanningError(f"no scripted plan for {request!r}") from exc
