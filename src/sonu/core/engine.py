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
from sonu.core.conversation import Conversation, Turn, results_of
from sonu.core.planning import Plan, Planner, PlanningError
from sonu.core.report import StepRecord, TaskReport
from sonu.core.responder import Responder
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.policy.engine import Policy
from sonu.tools.base import Choices, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.registry import MAX_STEPS, ToolRegistry
from sonu.world.origin import find_origin

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    description: str
    tool: str
    risk: Risk
    reason: str


Confirmer = Callable[[ConfirmRequest], Awaitable[bool]]
# Asks the user to pick one of the choices; returns the picked value, or None if they didn't answer.
Chooser = Callable[[Choices], Awaitable[str | None]]


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
        conversation: Conversation | None = None,
        responder: Responder | None = None,
    ) -> None:
        self.planner = planner
        self.registry = registry
        self.ctx = ctx
        self.policy = policy
        self.audit = audit
        # Without these (tests, `sonu eval`), every request stands alone and gets no spoken reply.
        self.conversation = conversation
        self.responder = responder

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
        choose: Chooser | None = None,
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
        history = self.conversation.recent() if self.conversation is not None else []
        self.ctx.request_text = request
        self.ctx.user_words = " ".join([*(turn.request for turn in history), request])
        self.ctx.recent_windows = self.conversation.last_windows() if self.conversation is not None else ()
        self.ctx.task_browser = None
        self.ctx.task_folder = None

        try:
            snapshot = await self.ctx.desktop.snapshot()
            origin = find_origin(snapshot.windows, self.ctx.ancestors(), snapshot.active_address)
            self.ctx.desktop.origin_address = origin
            snapshot = replace(snapshot, origin_address=origin)
            labels = self.ctx.resolver.labels_for_classes(w.window_class for w in snapshot.windows)
            plan = await self.planner.plan(
                request,
                snapshot,
                self.registry,
                app_labels=labels,
                history=history,
                notes=self.ctx.memory.notes(),
            )
        except (HyprlandError, PlanningError) as exc:
            report.error = str(exc)
            self._audit("task.end", {"status": "error", "error": str(exc)})
            return await self._finish(report, history)

        report.plan = plan
        self._audit(
            "task.plan",
            {
                "kind": plan.kind,
                "steps": [{"tool": step.tool, "args": step.args} for step in plan.steps],
                "unsupported": list(plan.unsupported),
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
                    index,
                    step.tool,
                    step.args,
                    confirm=confirm,
                    choose=choose,
                    dry_run=dry_run,
                    observer=observer,
                )
                report.steps.append(record)
                self._audit("step.result", record.audit_data())
                if observer:
                    observer.on_step(record)

        return await self._finish(report, history)

    async def _finish(self, report: TaskReport, history: list[Turn]) -> TaskReport:
        """The natural reply, and this request's place in the conversation."""
        if self.responder is not None:
            report.reply = await self.responder.reply(report, history)
        if report.plan is not None:
            self._audit("task.end", {"status": report.status, "reply": report.reply})
        if self.conversation is not None and not report.dry_run and report.plan is not None:
            said = report.reply or (report.plan.message if report.plan.kind != "steps" else report.headline())
            windows = tuple(
                str(address) for record in report.steps for address in record.outcome.data.get("windows", ())
            )
            self.conversation.add(report.request, said, results_of(report), windows)
        return report

    async def _run_step(
        self,
        index: int,
        tool_name: str,
        raw_args: dict[str, Any],
        *,
        confirm: Confirmer,
        choose: Chooser | None,
        dry_run: bool,
        observer: Observer | None,
    ) -> StepRecord:
        chosen: str | None = None

        def record(outcome: ToolOutcome, description: str = "", decision: Any = None) -> StepRecord:
            return StepRecord(index, tool_name, raw_args, description or tool_name, outcome, decision, chosen)

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
        if isinstance(prepared, ToolOutcome) and prepared.choices and choose is not None and not dry_run:
            # The step is ambiguous: let the user pick, then resolve it again on a fresh snapshot.
            choices = prepared.choices
            try:
                picked = await choose(choices)
            except Exception:  # e.g. stdin closed: treat as "no answer", never as a pick
                logger.exception("asking the user failed")
                picked = None
            if picked is None:
                return record(replace(prepared, summary=f"{prepared.summary} — no answer given"))
            if choices.label_for(picked) is None:  # only values SONU itself offered are accepted
                return record(ToolOutcome(StepStatus.FAILED, "that answer wasn't one of the choices"))
            chosen = picked
            try:
                snapshot = await self.ctx.desktop.snapshot()
                prepared = await tool.prepare(args, self.ctx, snapshot, choice=picked)
            except HyprlandError as exc:
                return record(ToolOutcome(StepStatus.FAILED, f"couldn't read the desktop: {exc}"))
            if isinstance(prepared, ToolOutcome) and prepared.status is StepStatus.NEEDS_INPUT:
                # Never ask twice in a row for the same step.
                prepared = replace(prepared, choices=None)
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
        decision = self.policy.decide(tool, tainted=True, risk=prepared.risk)
        risk = max(tool.risk, prepared.risk or tool.risk, key=lambda r: r.rank)
        if dry_run:
            outcome = ToolOutcome(StepStatus.SKIPPED, prepared.description)
            return record(outcome, prepared.description, decision)
        if decision.verdict == "deny":
            outcome = ToolOutcome(
                StepStatus.DENIED, f"Not allowed: {prepared.description} ({decision.reason})"
            )
            return record(outcome, prepared.description, decision)
        if decision.verdict == "confirm":
            approved = await confirm(ConfirmRequest(prepared.description, tool.name, risk, decision.reason))
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
