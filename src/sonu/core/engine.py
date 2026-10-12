"""The SONU loop: understand → plan → policy → execute → observe → verify → report.

Everything the engine does is recorded in the audit log. The engine — not the tools and not the
model — decides a step's final status, and it refuses to call a side-effecting step "done"
unless the step's own checks passed on a snapshot taken after the action.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from sonu.audit.log import AuditError, AuditLog
from sonu.core.conversation import Conversation, Turn, results_of
from sonu.core.corrections import corrected
from sonu.core.planning import Plan, Planner, PlanningError
from sonu.core.report import StepRecord, TaskReport
from sonu.core.responder import Responder
from sonu.core.talks import kept, told
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.policy.engine import Policy, outside_caller_risk
from sonu.tools.base import Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome, args_problem
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
    details: str | None = None  # shown in full before asking (e.g. a prompt SONU wrote)
    spoken: str | None = None  # what voice asks instead of the whole description


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


def recoverable(report: TaskReport) -> bool:
    """Worth a second try (D92): a step failed — and nothing was declined, denied or left waiting for the
    user (then their answer stands)."""
    if report.dry_run or report.plan is None or report.plan.kind != "steps":
        return False
    statuses = {record.outcome.status for record in report.steps}
    return StepStatus.FAILED in statuses and not statuses & {StepStatus.DENIED, StepStatus.NEEDS_INPUT}


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
        recover: bool = True,  # a second try when a step fails (D92)
    ) -> None:
        self.recover = recover
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
        said = request.strip()
        # "open kitty on 3, no, 4": the correction applied once, before anything reads the words (D89)
        request = corrected(said)
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
        start: dict[str, Any] = {"request": request, "dry_run": dry_run}
        if request != said:
            start["said"] = said  # the user's words as they came, before the correction
        self._audit("task.start", start)
        history = self.conversation.recent() if self.conversation is not None else []
        self.ctx.request_text = request
        self.ctx.user_words = " ".join([*(turn.request for turn in history), request])
        self.ctx.recent_windows = self.conversation.last_windows() if self.conversation is not None else ()
        self.ctx.task_browser = None
        self.ctx.task_folder = None
        self.ctx.task_windows = ()
        self.ctx.task_would_open = ()

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
            await self._run_steps(
                plan, report, confirm=confirm, choose=choose, dry_run=dry_run, observer=observer
            )
            if self.recover and recoverable(report):
                await self._try_another_way(report, labels, history, confirm, choose, observer)

        return await self._finish(report, history)

    async def _run_steps(
        self,
        plan: Plan,
        report: TaskReport,
        *,
        confirm: Confirmer,
        choose: Chooser | None,
        dry_run: bool,
        observer: Observer | None,
    ) -> None:
        done = {(r.tool, repr(r.args)) for r in report.steps if r.outcome.status is StepStatus.DONE}
        for step in plan.steps[: MAX_STEPS - len(report.steps)]:
            if (step.tool, repr(step.args)) in done:
                continue  # a second try never redoes what worked (D92)
            record = await self._run_step(
                len(report.steps),
                step.tool,
                step.args,
                confirm=confirm,
                choose=choose,
                dry_run=dry_run,
                observer=observer,
            )
            report.steps.append(record)
            self.ctx.task_windows += tuple(str(a) for a in record.outcome.data.get("windows", ()))
            self._audit("step.result", record.audit_data())
            if observer:
                observer.on_step(record)

    async def _try_another_way(
        self,
        report: TaskReport,
        labels: Mapping[str, str],
        history: list[Turn],
        confirm: Confirmer,
        choose: Chooser | None,
        observer: Observer | None,
    ) -> None:
        """A step failed (D92): plan once more with what happened — the user's words are the same, every rule
        and question applies again — and run what's still needed."""
        tried = results_of(report)  # private results stay "(private result)"
        try:
            snapshot = await self.ctx.desktop.snapshot()
            retry = await self.planner.plan(
                report.request,
                snapshot,
                self.registry,
                app_labels=labels,
                history=history,
                notes=self.ctx.memory.notes(),
                tried=tried,
            )
        except (HyprlandError, PlanningError) as exc:
            self._audit("task.retry", {"error": str(exc)})
            return
        self._audit(
            "task.retry",
            {
                "kind": retry.kind,
                "steps": [{"tool": s.tool, "args": s.args} for s in retry.steps],
                "model": retry.model,
            },
        )
        if retry.kind != "steps" or not retry.steps or len(report.steps) >= MAX_STEPS:
            return  # no other way: the failure stands, and is said
        report.recovered_from = len(report.steps)
        await self._run_steps(retry, report, confirm=confirm, choose=choose, dry_run=False, observer=observer)
        if report.recovered_from == len(report.steps):
            report.recovered_from = None  # nothing new ran

    async def call_tool(
        self,
        tool_name: str,
        raw_args: dict[str, Any],
        *,
        caller: str,
        confirm: Confirmer,
        choose: Chooser | None = None,
    ) -> StepRecord:
        """One tool step asked for by another program (Claude Code over MCP, D61), not by the user.

        The same path as a planned step — arguments checked, resolved on a fresh snapshot, the policy,
        verified — but there are no user's words: a step using words (a search, a name) or one that
        leaves the laptop, types or writes is asked on screen (``outside_caller_risk``).
        """

        def failed(summary: str) -> StepRecord:
            return StepRecord(0, tool_name, raw_args, tool_name, ToolOutcome(StepStatus.FAILED, summary))

        if self.audit is not None:
            verification = self.audit.verify()
            if not verification.ok:
                return failed(
                    f"the audit log is damaged (line {verification.line}: {verification.error}); "
                    "SONU won't act until it's fixed — see `sonu audit verify`"
                )
        self._audit("call.start", {"caller": caller, "tool": tool_name, "args": raw_args})
        self.ctx.request_text = ""
        # What grounding checks text against: the caller's own arguments — the step is asked instead.
        self.ctx.user_words = " ".join(
            str(value) for value in raw_args.values() if isinstance(value, str | int)
        )
        self.ctx.recent_windows = ()
        self.ctx.task_browser = None
        self.ctx.task_folder = None
        self.ctx.task_windows = ()
        self.ctx.task_would_open = ()
        tool = self.registry.get(tool_name)
        try:
            snapshot = await self.ctx.desktop.snapshot()
            origin = find_origin(snapshot.windows, self.ctx.ancestors(), snapshot.active_address)
            self.ctx.desktop.origin_address = origin
        except HyprlandError as exc:
            record = failed(f"couldn't read the desktop: {exc}")
        else:
            record = await self._run_step(
                0,
                tool_name,
                raw_args,
                confirm=confirm,
                choose=choose,
                dry_run=False,
                observer=None,
                least_risk=outside_caller_risk(tool, raw_args) if tool is not None else None,
            )
        self._audit("step.result", record.audit_data())
        return record

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
        if self.ctx.talks is not None and kept(report):  # remembered past the conversation (D86)
            try:
                self.ctx.talks.add(report.request, told(report), self.ctx.now().timestamp())
            except sqlite3.Error as exc:  # a full disk mustn't fail the request
                logger.warning("couldn't keep the conversation: %s", exc)
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
        least_risk: Risk | None = None,
    ) -> StepRecord:
        chosen: str | None = None

        def record(outcome: ToolOutcome, description: str = "", decision: Any = None) -> StepRecord:
            return StepRecord(index, tool_name, raw_args, description or tool_name, outcome, decision, chosen)

        tool = self.registry.get(tool_name)
        if tool is None:
            return record(ToolOutcome(StepStatus.FAILED, f"the plan used an unknown tool '{tool_name}'"))
        try:
            args = tool.parse_args(raw_args)
        except ValueError as exc:
            return record(
                ToolOutcome(
                    StepStatus.FAILED, f"the plan gave {tool_name} invalid arguments ({args_problem(exc)})"
                )
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
        if least_risk is not None:
            prepared = replace(
                prepared, risk=max(prepared.risk or least_risk, least_risk, key=lambda r: r.rank)
            )
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
            if Effect.APP_LAUNCH in tool.effects:  # what a later step's "it" would be (screen.type)
                self.ctx.task_would_open += (prepared.description,)
            outcome = ToolOutcome(StepStatus.SKIPPED, prepared.description)
            return record(outcome, prepared.description, decision)
        if decision.verdict == "deny":
            outcome = ToolOutcome(
                StepStatus.DENIED, f"Not allowed: {prepared.description} ({decision.reason})"
            )
            return record(outcome, prepared.description, decision)
        if decision.verdict == "confirm":
            approved = await confirm(
                ConfirmRequest(
                    prepared.description, tool.name, risk, decision.reason, prepared.details, prepared.spoken
                )
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
