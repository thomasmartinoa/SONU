"""The SONU loop: understand → plan → policy → execute → observe → verify → report.

Everything the engine does is recorded in the audit log. The engine — not the tools and not the
model — decides a step's final status, and it refuses to call a side-effecting step "done"
unless the step's own checks passed on a snapshot taken after the action.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from sonu.audit.log import AuditError, AuditLog
from sonu.core.planning import Plan, Planner, PlanningError
from sonu.core.report import StepRecord, TaskReport
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.policy.engine import Policy
from sonu.tools.base import Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.registry import MAX_STEPS, ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    description: str
    tool: str
    risk: Risk
    reason: str


Confirmer = Callable[[ConfirmRequest], Awaitable[bool]]


class Observer:
    """Receives progress while a task runs (the terminal UI subclasses this). Defaults do nothing."""

    def on_plan(self, plan: Plan) -> None:
        """The planner produced a plan."""

    def on_step_start(self, index: int, description: str) -> None:
        """A step is about to run (after any confirmation)."""

    def on_step(self, record: StepRecord) -> None:
        """A step finished (or was stopped before running)."""


def enforce_verification(tool: Tool, outcome: ToolOutcome) -> ToolOutcome:
    """A side-effecting step is only DONE when it has checks and all of them passed."""
    if tool.risk is Risk.READ_ONLY or outcome.status is not StepStatus.DONE:
        return outcome
    if not outcome.checks:
        return replace(outcome, status=StepStatus.UNVERIFIED, summary=outcome.summary + " (not verified)")
    if not all(check.passed for check in outcome.checks):
        failed = "; ".join(check.description for check in outcome.checks if not check.passed)
        return replace(
            outcome, status=StepStatus.FAILED, summary=f"{outcome.summary} — but check failed: {failed}"
        )
    return outcome


class Engine:
    def __init__(
        self,
        *,
        planner: Planner,
        registry: ToolRegistry,
        ctx: ToolContext,
        policy: Policy,
        audit: AuditLog | None = None,
    ) -> None:
        self.planner = planner
        self.registry = registry
        self.ctx = ctx
        self.policy = policy
        self.audit = audit

    def _audit(self, kind: str, data: dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(kind, data)
        except (OSError, AuditError) as exc:
            # Fail closed happens *before* acting (see run); here we only log.
            logger.error("audit write failed: %s", exc)

    async def run(
        self,
        request: str,
        *,
        confirm: Confirmer,
        dry_run: bool = False,
        observer: Observer | None = None,
    ) -> TaskReport:
        request = request.strip()
        report = TaskReport(request=request, dry_run=dry_run)
        if not request:
            report.error = "empty request"
            return report
        if self.audit is not None and not dry_run:
            verification = self.audit.verify()
            if not verification.ok:
                report.error = (
                    f"the audit log is damaged (line {verification.line}: {verification.error}); "
                    "SONU won't act until it's fixed — see `sonu audit verify`"
                )
                return report
        self._audit("task.start", {"request": request, "dry_run": dry_run})

        try:
            snapshot = await self.ctx.desktop.snapshot()
            plan = await self.planner.plan(request, snapshot, self.registry)
        except (HyprlandError, PlanningError) as exc:
            report.error = str(exc)
            self._audit("task.end", {"status": "error", "error": str(exc)})
            return report

        report.plan = plan
        self._audit(
            "task.plan",
            {
                "kind": plan.kind,
                "steps": [{"tool": step.tool, "args": step.args} for step in plan.steps],
                "model": plan.model,
                "duration_s": round(plan.duration_s, 3),
                "attempts": plan.attempts,
            },
        )
        if observer:
            observer.on_plan(plan)

        if plan.kind == "steps":
            for index, step in enumerate(plan.steps[:MAX_STEPS]):
                record = await self._run_step(
                    index, step.tool, step.args, confirm=confirm, dry_run=dry_run, observer=observer
                )
                report.steps.append(record)
                self._audit("step.result", record.audit_data())
                if observer:
                    observer.on_step(record)

        self._audit("task.end", {"status": report.status})
        return report

    async def _run_step(
        self,
        index: int,
        tool_name: str,
        raw_args: dict[str, Any],
        *,
        confirm: Confirmer,
        dry_run: bool,
        observer: Observer | None,
    ) -> StepRecord:
        def record(outcome: ToolOutcome, description: str = "", decision: Any = None) -> StepRecord:
            return StepRecord(index, tool_name, raw_args, description or tool_name, outcome, decision)

        tool = self.registry.get(tool_name)
        if tool is None:
            return record(ToolOutcome(StepStatus.FAILED, f"the plan used an unknown tool '{tool_name}'"))
        try:
            args = tool.Args.model_validate(raw_args)
        except ValidationError as exc:
            problem = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
            return record(
                ToolOutcome(StepStatus.FAILED, f"the plan gave {tool_name} invalid arguments ({problem})")
            )

        try:
            snapshot = await self.ctx.desktop.snapshot()  # fresh: earlier steps changed the desktop
            prepared = await tool.prepare(args, self.ctx, snapshot)
        except HyprlandError as exc:
            return record(ToolOutcome(StepStatus.FAILED, f"couldn't read the desktop: {exc}"))
        if isinstance(prepared, ToolOutcome):
            return record(prepared)
        return await self._decide_and_execute(
            index, tool, prepared, record, confirm=confirm, dry_run=dry_run, observer=observer
        )

    async def _decide_and_execute(
        self,
        index: int,
        tool: Tool,
        prepared: Prepared,
        record: Callable[..., StepRecord],
        *,
        confirm: Confirmer,
        dry_run: bool,
        observer: Observer | None,
    ) -> StepRecord:
        decision = self.policy.decide(tool, tainted=True)
        if dry_run:
            outcome = ToolOutcome(StepStatus.SKIPPED, prepared.description)
            return record(outcome, prepared.description, decision)
        if decision.verdict == "deny":
            outcome = ToolOutcome(
                StepStatus.DENIED, f"Not allowed: {prepared.description} ({decision.reason})"
            )
            return record(outcome, prepared.description, decision)
        if decision.verdict == "confirm":
            approved = await confirm(
                ConfirmRequest(prepared.description, tool.name, tool.risk, decision.reason)
            )
            if not approved:
                outcome = ToolOutcome(StepStatus.DENIED, f"Skipped (not approved): {prepared.description}")
                return record(outcome, prepared.description, decision)
        if observer:
            observer.on_step_start(index, prepared.description)
        try:
            outcome = await tool.execute(prepared, self.ctx)
        except HyprlandError as exc:
            outcome = ToolOutcome(StepStatus.FAILED, f"{prepared.description} failed: {exc}")
        except Exception:  # a bug in a tool must not crash the loop or leave the step unreported
            logger.exception("tool %s crashed", tool.name)
            outcome = ToolOutcome(StepStatus.FAILED, f"{prepared.description} failed with an internal error")
        return record(enforce_verification(tool, outcome), prepared.description, decision)
