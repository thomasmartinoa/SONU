"""`sonu voice check`: how well speech recognition hears *this* user — their voice, accent and mic.

The user reads a few requests like the ones they give SONU; each recording is transcribed by every
model being compared, and the words are scored against what they were asked to read. The audio
stays in memory and is dropped at the end. Found on the laptop: with the owner's voice, "Hey Sonu"
came out as "his own", "Hey Soin", "Hey Sovenue" — which model hears them best can only be measured
with their voice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sonu.voice.wake import fix_name

SENTENCES = (
    "Listen for Hey Sonu.",
    "Open kitty on workspace two.",
    "Play Alone by Marshmello on YouTube.",
    "Move the browser to workspace four.",
    "Turn the volume down a little and pause the music.",
    "Yes.",
    "Remind me in ten minutes to drink water.",
    "What's my battery right now?",
    "Take a screenshot of this window.",
    "No.",
)

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
_NUMBERS = {str(n): word for n, word in enumerate(_NUMBER_WORDS)}


def words(text: str) -> list[str]:
    """Lowercase words, punctuation dropped, small numbers as words ("2" = "two"), the name fixed, and
    words Whisper wrote together split ("OpenKitty")."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", fix_name(text))
    found = re.findall(r"[a-z0-9']+", text.lower().replace("\u2019", "'"))
    return [_NUMBERS.get(word, word) for word in found]


def word_errors(expected: str, heard: str) -> int:
    """Words substituted, missed or added (word-level edit distance)."""
    want, got = words(expected), words(heard)
    row = list(range(len(got) + 1))
    for i, w in enumerate(want, 1):
        previous, row[0] = row[0], i
        for j, g in enumerate(got, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (w != g))
    return row[-1]


@dataclass
class Score:
    model: str
    errors: int = 0
    total: int = 0
    seconds: float = 0.0
    heard: list[str] = field(default_factory=list)

    def add(self, expected: str, heard: str, seconds: float) -> None:
        self.errors += word_errors(expected, heard)
        self.total += len(words(expected))
        self.seconds += seconds
        self.heard.append(heard)

    @property
    def accuracy(self) -> float:
        return max(0.0, 1 - self.errors / self.total) if self.total else 0.0

    @property
    def mean_seconds(self) -> float:
        return self.seconds / len(self.heard) if self.heard else 0.0
