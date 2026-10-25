"""Choosing from SONU's list by voice (phase 2, research §3): which file, which app, which package.

Found on the laptop: by voice, a choice only showed buttons on the HUD and nothing listened — "2 packages
could be 'cozy' — no answer given". Now SONU says how many there are and the first few, and listens for "the
second one", "number three", "the last one", "the one in Downloads" (a word only that option has), or "none".

* Only an offered option counts, as with the buttons; anything unclear is asked once more, then left to the
  buttons (which work at the same time — the first answer wins).
* A choice only says *which* — the step is still checked by the policy afterwards, so a high-risk step still
  needs its OK on the screen (T5): a voice choice never approves anything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sonu.tools.base import Choice, Choices
from sonu.voice.meant import _EVERYDAY, _HEARD_FOR, sound_key

_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5,
    "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10,
}  # fmt: skip
_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, **{str(n): n for n in range(1, 100)},
}  # fmt: skip
_SAID_ORDINALS = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
)
_NONE = re.compile(r"\b(?:none|nothing|neither|cancel|never ?mind|forget it)\b")
_JUST_NO = re.compile(r"^\W*(?:no|nope|no thanks?)\W*$")
# "number three", "No. 3" (Whisper's way of writing it), "option two"
_NUMBERED = re.compile(r"\b(?:number|no|option)\b\W*(\w+)")
_SPOKEN_AT_MOST = 5  # more than this: the count and the first three are said, the rest are on the screen
_SAID_FIRST = 3


@dataclass(frozen=True, slots=True)
class Picked:
    kind: Literal["pick", "none", "unclear"]
    value: str | None = None


def said_label(label: str) -> str:
    """The part of a label worth saying: "cozy-stack — extra: Digital home…" → "cozy-stack"."""
    return " ".join(label.split(" — ")[0].split())[:60]


def spoken_list(choices: Choices) -> str:
    """What SONU says: the question, how many, the first few — "Which package for 'cozy'? There are two:
    first, cozy-stack; second, dunesh. Say which one, or none."."""
    labels = [said_label(option.label) for option in choices.options]
    count = len(labels)
    named = labels if count <= _SPOKEN_AT_MOST else labels[:_SAID_FIRST]
    listed = "; ".join(f"{_SAID_ORDINALS[i]}, {label}" for i, label in enumerate(named))
    if count == 1:
        return f"{choices.prompt} I found one: {labels[0]}. Say first to choose it, or none."
    rest = " — the rest are on the screen" if count > len(named) else ""
    return f"{choices.prompt} There are {count}: {listed}{rest}. Say which one, or none."


def hint_for(choices: Choices) -> str:
    """Words the user may say: the options' names and the ordinals (given to the local ears only — a label
    can be a window title, and window content never leaves the machine)."""
    return ", ".join(["first, second, third, last, none", *(said_label(o.label) for o in choices.options)])


def pick(heard: str, options: Sequence[Choice]) -> Picked:
    """Which offered option ``heard`` names, "none", or unclear — never anything that wasn't offered."""
    text = heard.lower().strip()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return Picked("unclear")
    if _NONE.search(text):
        return Picked("none")
    count = len(options)

    def nth(number: int) -> Picked:
        return Picked("pick", options[number - 1].value) if 1 <= number <= count else Picked("unclear")

    ordinals = {_ORDINALS[w] for w in words if w in _ORDINALS} | ({count} if "last" in words else set())
    if len(ordinals) == 1:
        return nth(ordinals.pop())
    if len(ordinals) > 1:
        return Picked("unclear")  # "not the first, the second": asked again
    if said := _NUMBERED.search(text):
        number = _NUMBERS.get(said.group(1)) or _HEARD_FOR.get(said.group(1))
        if number is not None:
            return nth(number)
    content = [w for w in words if w not in ("the", "a", "an", "one", "please", "that", "this", "it")]
    if len(content) == 1 and (number := _NUMBERS.get(content[0])) is not None:
        return nth(number)  # "Two."
    if _JUST_NO.match(text):
        return Picked("none")
    return _by_name(words, options)


def _by_name(words: list[str], options: Sequence[Choice]) -> Picked:
    """ "The one in Downloads": a word only one option's label has (or sounds like)."""
    labels = [set(re.findall(r"[a-z0-9]+", said_label(o.label).lower())) for o in options]
    meant = [w for w in words if len(w) >= 3 and w not in _EVERYDAY and w not in _ORDINALS]
    found: set[int] = set()
    for word in meant:
        exact = {i for i, label in enumerate(labels) if word in label}
        alike = {
            i
            for i, label in enumerate(labels)
            if len(sound_key(word)) >= 4 and any(sound_key(word) == sound_key(other) for other in label)
        }
        hits = exact or alike
        if len(hits) == 1:
            found |= hits
    whole = sound_key("".join(meant))  # "Cow say" is cowsay: the words run together, as a name is
    if not found and len(whole) >= 4:
        found = {
            i
            for i, option in enumerate(options)
            if sound_key(re.sub(r"[^a-z0-9]", "", said_label(option.label).lower())) == whole
        }
    return Picked("pick", options[found.pop()].value) if len(found) == 1 else Picked("unclear")
