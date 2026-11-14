"""A second opinion on a name nothing matched (D111, phase 2).

Measured on the owner's voice (`sonu voice bench`, D106): local Whisper turbo got 14 of 32 names right, Sarvam
20, Gemini 3.5 Transcribe 26 — but Gemini is a cloud service and takes ~4 s, so it never hears everything.
It listens again only when it is worth it: **the first step of a spoken request failed because a name matched
nothing on this laptop** ("'Cloud Code' is not installed", "couldn't find a folder called 'Dubworks'"). Then
nothing has run yet, so trying again with a better transcript can't repeat anything.

The sound matching of D107 has already run: this is for the names it couldn't place.
"""

from __future__ import annotations

from sonu.core.report import TaskReport
from sonu.tools.base import StepStatus


def unmatched_name(report: TaskReport) -> str | None:
    """The name the request's first step couldn't find, if that is what stopped it — else None."""
    if report.error or not report.steps:
        return None
    first = report.steps[0].outcome
    if first.status is not StepStatus.FAILED:
        return None
    name = first.data.get("unresolved")
    return str(name) if name else None


def worth_trying_again(before: str, after: str) -> bool:
    """The second listening heard something else (not only another capital or full stop)."""

    def plain(text: str) -> str:
        return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())

    return bool(plain(after)) and plain(before) != plain(after)
