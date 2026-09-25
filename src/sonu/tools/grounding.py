"""Checking that text a step uses came from the user's own request, not from the model or the screen.

Used wherever a model-written string would otherwise pick something consequential: a web search
(data leaving the machine), a Wi-Fi network or a Bluetooth device to connect to.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from sonu.desktop.apps.resolver import normalize

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
