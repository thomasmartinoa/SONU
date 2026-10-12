"""A correction mid-sentence (D89): "open kitty on 3, no, 4", "play lofi on spotify, actually on youtube".

People correct themselves while they talk. The planner model mostly understands it, but SONU's own rules read
the user's words too (found on the laptop: "play lofi on spotify, actually on youtube" played on Spotify —
"spotify" in the words decides the service, D41). So the request is corrected once, before anything reads it:
the corrected part is replaced by what came after the marker, and everything else stays the user's own words.

Careful by design — when unsure, the words are left as they were (the model still sees them all):

* A marker after a comma ("…, sorry, …", "…, actually …", "…, no, …"), or one that can't be anything else
  ("I mean", "wait no", "no wait", "or rather", "make that").
* The correction is short (at most four words, up to the next "," / "and" / "then").
* It replaces from the last place its first word was said ("on spotify" ← "on youtube"), or else the last word
  ("firefox" ← "zen") — not when it starts with a small word ("sorry to bother you"), and after a bare "no"
  only a number for a number ("at 5, no, 6 to call mom") or a word said before.
"""

from __future__ import annotations

import re

_MARKER = re.compile(
    r"(?:,\s*(?P<soft>no|sorry|actually)"
    r"|(?:,\s*|\s+)(?P<hard>no,?\s+wait|wait,?\s+no|i mean|or rather|make that))"
    r"(?:,\s*|\s+)",
    re.IGNORECASE,
)
_DEFINING = re.compile(r"\b(?:say|says|said|call|by)\b", re.IGNORECASE)
_CLAUSE_END = re.compile(r",|\s+(?:and|then)\s+", re.IGNORECASE)
_SMALL = frozenset(
    [
        "to",
        "for",
        "if",
        "but",
        "that",
        "it",
        "i",
        "you",
        "we",
        "can",
        "could",
        "would",
        "please",
        "about",
        "just",
        "and",
        "or",
        "so",
        "do",
        "don't",
        "not",
        "never",
        "what",
        "how",
        "why",
        "when",
        "is",
        "are",
        "was",
        "it's",
    ]
)
MAX_WORDS = 4


def _key(word: str) -> str:
    return word.strip(".,!?;:'\"").casefold()


def _number(word: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?%?", word.strip(".,!?;:")))


def _apply(text: str, marker: re.Match[str]) -> str | None:
    before, after = text[: marker.start()].split(), text[marker.end() :]
    if (marker.group("hard") or "").lower() == "i mean" and _DEFINING.search(text[: marker.start()]):
        return None  # "when I say VS Code I mean Code OSS" defines a name (found by the golden evals)
    if before and after.split() and _number(before[-1]) and _number(after.split()[0]):
        # "remind me at 5, no, 6 to call mom": the number, and the sentence goes on
        number = after.split()[0]
        return " ".join([*before[:-1], number]) + after.partition(number)[2]
    end = _CLAUSE_END.search(after)
    correction, rest = (after[: end.start()], after[end.start() :]) if end else (after, "")
    words = correction.split()
    if not before or not words or len(words) > MAX_WORDS:
        return None
    first = _key(words[0])
    said = [_key(word) for word in before]
    if first in said:
        cut = len(said) - 1 - said[::-1].index(first)  # from where it was said last
    elif marker.group("soft") and marker.group("soft").lower() == "no":
        return None  # "open kitty and no, I don't need…": not a correction
    elif first in _SMALL or _number(words[0]) != _number(before[-1]):
        return None  # "open kitty, sorry to bother you"
    else:
        cut = len(before) - 1  # "firefox, I mean zen": the last word
    return " ".join([*before[:cut], *words]) + rest


def corrected(text: str) -> str:
    """The request with each correction applied; unchanged when there is none (or it's unclear)."""
    for _ in range(3):  # "3, no, 4, sorry, 5"
        changed = next(
            (fixed for marker in _MARKER.finditer(text) if (fixed := _apply(text, marker)) is not None), None
        )
        if changed is None:
            break
        text = changed
    return text
