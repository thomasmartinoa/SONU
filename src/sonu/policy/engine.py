"""Deciding whether a step may run: allow, ask the user first, or deny.

Decisions depend on the tool's declared risk, the user's autonomy mode, and whether untrusted
text could have influenced the plan ("taint"). In milestone 1 the planner always sees window
titles — text written by apps and websites — so every plan counts as tainted, and anything
riskier than LOW (e.g. closing windows) always needs confirmation, whatever the mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sonu.config.settings import AutonomyMode
from sonu.tools.base import Effect, Risk, Tool

Verdict = Literal["allow", "confirm", "deny"]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    verdict: Verdict
    reason: str


class Policy:
    def __init__(self, autonomy: AutonomyMode) -> None:
        self.autonomy = autonomy

    def decide(self, tool: Tool, *, tainted: bool, risk: Risk | None = None) -> PolicyDecision:
        """``risk`` can raise (never lower) the tool's own risk for one step."""
        risk = max(tool.risk, risk or tool.risk, key=lambda r: r.rank)
        if risk is Risk.FORBIDDEN:
            return PolicyDecision("deny", f"{tool.name} is never allowed")
        if risk is Risk.READ_ONLY:
            return PolicyDecision("allow", "read-only")
        if self.autonomy == "suggest":
            return PolicyDecision("deny", "autonomy is 'suggest': SONU only shows plans")
        if self.autonomy == "confirm-each":
            return PolicyDecision("confirm", "autonomy is 'confirm-each'")
        if risk is Risk.HIGH:
            return PolicyDecision("confirm", "high-risk action")
        if risk is Risk.MEDIUM:
            if self.autonomy == "trusted" and not tainted:
                return PolicyDecision("allow", "medium risk, trusted mode")
            reason = "medium-risk action"
            if self.autonomy == "trusted":
                reason += " (the plan may have been influenced by window titles)"
            return PolicyDecision("confirm", reason)
        return PolicyDecision("allow", "low-risk action")


# What another program (Claude Code over MCP) can't do without the user saying yes on screen: anything
# that leaves the laptop, types, writes or removes — even when it's low risk by voice.
ASK_OUTSIDE = frozenset(
    {
        Effect.WEB_OPEN,
        Effect.TERMINAL_INPUT,
        Effect.UI_INPUT,
        Effect.GITHUB_WRITE,
        Effect.FILE_WRITE,
        Effect.INSTALL,
        Effect.UNINSTALL,
        Effect.POWER,
        Effect.WINDOW_CLOSE,
    }
)


def outside_caller_risk(tool: Tool, args: Mapping[str, Any]) -> Risk | None:
    """The least risk of a step another program asked for (D61): there are no user's words to check
    a search or a name against, so a step using such words — or one in ``ASK_OUTSIDE`` — is asked."""
    words = any(isinstance(args.get(name), str) and args[name].strip() for name in tool.grounded)
    return Risk.MEDIUM if words or tool.effects & ASK_OUTSIDE else None
