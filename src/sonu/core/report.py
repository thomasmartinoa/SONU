"""The task report: built only from what was actually verified.

This is where "never claim success without evidence" is visible to the user: every line comes
from a step's final status and checks, never from the plan or from what a tool intended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sonu.core.planning import Plan
from sonu.policy.engine import PolicyDecision
from sonu.tools.base import StepStatus, ToolOutcome

TaskStatus = Literal[
    "done",
    "unverified",
    "planned",
    "partial",
    "failed",
    "declined",
    "needs-input",
    "ask",
    "reply",
    "cannot",
    "error",
]

ICONS = {
    StepStatus.DONE: "✔",
    StepStatus.UNVERIFIED: "▲",
    StepStatus.FAILED: "✖",
    StepStatus.NEEDS_INPUT: "?",
    StepStatus.DENIED: "⊘",
    StepStatus.SKIPPED: "·",
}


@dataclass(frozen=True, slots=True)
class StepRecord:
    index: int
    tool: str
    args: dict[str, Any]
    description: str
    outcome: ToolOutcome
    decision: PolicyDecision | None = None
    # The value the user picked when the step asked (a desktop id or window address), if any.
    choice: str | None = None

    @property
    def asked(self) -> bool:
        """The step needed the user's approval (so a DENIED step means "not approved", not "forbidden")."""
        return self.decision is not None and self.decision.verdict == "confirm"

    def audit_data(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "args": self.args,
            "description": self.description,
            "decision": {"verdict": self.decision.verdict, "reason": self.decision.reason}
            if self.decision
            else None,
            "choice": self.choice,
            "status": self.outcome.status.value,
            "summary": "(personal content not logged)" if self.outcome.private else self.outcome.summary,
            "checks": [check.as_dict() for check in self.outcome.checks],
        }


@dataclass(slots=True)
class TaskReport:
    request: str
    plan: Plan | None = None
    steps: list[StepRecord] = field(default_factory=list)
    error: str | None = None
    dry_run: bool = False

    @property
    def status(self) -> TaskStatus:
        if self.error or self.plan is None:
            return "error"
        if self.plan.kind != "steps":
            return self.plan.kind
        statuses = [record.outcome.status for record in self.steps]
        if not statuses:
            return "cannot"
        if StepStatus.NEEDS_INPUT in statuses:
            return "needs-input"
        if self.dry_run:
            ok = all(s in (StepStatus.SKIPPED, StepStatus.DONE) for s in statuses)
            return "planned" if ok else "failed"
        if all(status is StepStatus.DONE for status in statuses):
            # Every step worked — but if part of the request had no tool, the request isn't done.
            return "partial" if self.plan.unsupported else "done"
        if all(status in (StepStatus.DONE, StepStatus.UNVERIFIED) for status in statuses):
            # Everything ran and nothing failed, but not every result could be confirmed (a suspend
            # SONU can't watch, a page in a background tab). Not "done" — and not "not done" either.
            return "partial" if self.plan.unsupported else "unverified"
        if StepStatus.DONE in statuses:
            return "partial"
        if all(record.outcome.status is StepStatus.DENIED and record.asked for record in self.steps):
            return "declined"
        return "failed"

    @property
    def not_done(self) -> list[str]:
        """Lines for the parts of the request SONU had no tool for."""
        if self.plan is None or self.plan.kind != "steps":
            return []
        return [f"✖ Can't do this yet: {part}" for part in self.plan.unsupported]

    @property
    def questions(self) -> list[str]:
        return [record.outcome.question for record in self.steps if record.outcome.question]

    def lines(self) -> list[str]:
        """Plain-text report, one line per step plus any message or question."""
        out: list[str] = []
        if self.error:
            return [f"✖ {self.error}"]
        if self.plan is None:
            return ["✖ no plan"]
        if self.plan.kind != "steps" or not self.steps:
            return [self.plan.message or "I couldn't turn that into actions."]
        for record in self.steps:
            summary = record.outcome.summary
            if self.dry_run and record.outcome.status is StepStatus.SKIPPED:
                summary = f"would: {record.description}"
            out.append(f"{ICONS[record.outcome.status]} {summary}")
        out.extend(self.not_done)
        out.extend(f"? {question}" for question in self.questions)
        return out

    def as_dict(self) -> dict[str, Any]:
        plan = self.plan
        return {
            "request": self.request,
            "status": self.status,
            "headline": self.headline(),
            "dry_run": self.dry_run,
            "error": self.error,
            "plan": None
            if plan is None
            else {
                "kind": plan.kind,
                "message": plan.message,
                "model": plan.model,
                "duration_s": round(plan.duration_s, 3),
                "attempts": plan.attempts,
                "unsupported": list(plan.unsupported),
            },
            "steps": [
                {
                    "tool": record.tool,
                    "args": record.args,
                    "description": record.description,
                    "status": record.outcome.status.value,
                    "summary": record.outcome.summary,
                    "question": record.outcome.question,
                    "choice": record.choice,
                    "checks": [check.as_dict() for check in record.outcome.checks],
                }
                for record in self.steps
            ],
            "questions": self.questions,
        }

    def headline(self) -> str:
        if self.dry_run and self.status == "failed":
            return "Dry run — some steps would fail"
        return {
            "done": "Done — every step verified",
            "unverified": "Done, but not confirmed — see the steps above",
            "planned": "Dry run — nothing was changed",
            "partial": "Partly done — see the steps below",
            "failed": "Not done",
            "declined": "Nothing changed — not approved",
            "needs-input": "I need an answer before continuing",
            "ask": "Question",
            "reply": "SONU",
            "cannot": "Can't do that",
            "error": "Error",
        }[self.status]
