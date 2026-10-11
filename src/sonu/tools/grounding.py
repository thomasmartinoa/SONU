"""Checking that text a step uses came from the user's own request, not from the model or the screen.

Used wherever a model-written string would otherwise pick something consequential: a web search
(data leaving the machine), a Wi-Fi network or a Bluetooth device to connect to.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from sonu.desktop.apps.resolver import normalize
from sonu.tools.base import StepStatus, ToolOutcome

if TYPE_CHECKING:
    from sonu.tools.context import ToolContext

# Small words a model may add without changing what the user asked for.
CONNECTIVES = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "how", "what", "my"}


def ungrounded_words(text: str, request: str) -> list[str]:
    """Words of ``text`` that don't come from the user's request (small spelling fixes allowed)."""
    request_words = set(normalize(request).split())
    missing = []
    for word in normalize(text).split():
        if word in request_words or word in CONNECTIVES:
            continue
        if len(word) >= 4 and any(SequenceMatcher(None, word, r).ratio() >= 0.8 for r in request_words):
            continue
        missing.append(word)
    return missing


def not_own_words(ctx: ToolContext, *values: str | None) -> ToolOutcome | None:
    """A refusal when any of ``values`` has words that aren't the user's (in this request or the recent ones),
    else None. Shared by the tools that act on the user's words for a tab, a file, a song, a page's field."""
    for value in values:
        if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
            return ToolOutcome(StepStatus.FAILED, f"That must be your own words ({', '.join(missing)} isn't)")
    return None
