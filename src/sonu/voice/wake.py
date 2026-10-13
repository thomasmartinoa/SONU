""" "Hey Sonu": listening for SONU's name, when the user turns it on (D47).

Off by default. While it's on, the microphone stays open in ``sonud`` and:

* a speech detector (Silero VAD) looks at the last 1.5 s of audio every 300 ms — nothing else runs
  while nobody talks;
* once someone has talked for ~1.8 s (or stopped sooner), only that beginning is transcribed, with
  "Hey Sonu" as the hint. If it doesn't **start** with SONU's name, the rest of what they say is
  ignored: never transcribed, never kept;
* if it does, SONU keeps recording until they stop, and the words after the name are the request
  ("Hey Sonu, open kitty on 2"). The name alone ("Hey Sonu") → SONU greets and listens, like the key.

Measured on the laptop: with the hint, Whisper wrote "Hey Sonu" / "Sonu, …" for three voices; the
hint also turned "I told Sonny about it" into "I told Sonu about it" — which is why the name only
counts at the start. Audio lives in memory and is dropped after each check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from sonu.voice.audio import DETECT_EVERY, FRAME_BYTES, FRAME_MS, RATE, RECORD_ARGV, SpeechDetector, frames

WAKE_HINT = "Hey Sonu"
_WAKE = re.compile(r"^\W*(?:(?:hey|hi|hello|ok(?:ay)?|yo)\W+)?(?:sonu|sanu|sonoo|sonnu)\b\W*", re.I)

WINDOW_FRAMES = 1500 // FRAME_MS  # the detector looks at the last 1.5 s
HEAD_FRAMES = 1800 // FRAME_MS  # the first 1.8 s of speech are checked for the name
PRE_ROLL_FRAMES = 500 // FRAME_MS  # audio kept from just before speech was noticed


# Found on the laptop, the owner's voice through the built-in mic: "Hey Sovenue", "Hey Soinu",
# "He Sonu", "Hey Soenu", "Hey Soin". After a greeting, a word close enough to "Sonu" is the name.
# "his": found with `sonu voice check` — the owner's "Listen for Hey Sonu" as "Listen for his sonu".
_CALLED = re.compile(r"\b(?:hey|hay|he|his|hi|hello|ok(?:ay)?|yo)([\s,.!]+)([a-z']+)", re.I)
_EXACT = {"sonu", "sanu", "sonoo", "sonnu"}
_WORDS = {"son", "sons", "soon", "sun", "song", "sonny", "sunny", "some", "soul", "sound", "sure", "sony"}
CLOSE = 0.7  # "Soin" 0.75, "Sovenue" 0.73, "Soenu" 0.89 — "Sonny" 0.67, "Sunny" 0.44


# Learned from the owner's own voice (D100, `sonu voice train-wake`): how Whisper writes their "Sonu", and the
# greeting before it — only what never turned up in their look-alike phrases. Loaded by sonud.
LEARNED_NAMES: set[str] = set()
LEARNED_GREETINGS: set[str] = set()
LEARNED_ALONE: set[str] = set()  # the name heard as one word at the start ("Hasonu")
_GREETINGS = ("hey", "hay", "he", "his", "hi", "hello", "ok", "okay", "yo")
LEARNED_FILE = "wake-learned.json"  # in the voice folder


def sounds_like_sonu(word: str) -> bool:
    w = word.lower().removesuffix("'s")
    if w in _EXACT or w in LEARNED_NAMES:
        return True
    if w in _WORDS or not w.startswith("s") or not 3 <= len(w) <= 8:
        return False
    return SequenceMatcher(None, w, "sonu").ratio() >= CLOSE


# Whisper sometimes writes the two words as one: "HeySanu", "HiSonu".
_JOINED = re.compile(r"\b(?:Hey|Hi|Hay)(S[a-z']+)\b")


def _called() -> re.Pattern[str]:
    if not LEARNED_GREETINGS:
        return _CALLED
    greetings = sorted({*_GREETINGS, *LEARNED_GREETINGS}, key=len, reverse=True)
    return re.compile(rf"\b(?:{'|'.join(map(re.escape, greetings))})([\s,.!]+)([a-z']+)", re.I)


def fix_name(text: str) -> str:
    """SONU's name as Whisper misheard it, after a greeting, written right: "Listen for Hey Soin" →
    "Listen for Hey Sonu". Other words are left alone."""
    text = _JOINED.sub(lambda m: "Hey Sonu" if sounds_like_sonu(m.group(1)) else m.group(0), text)
    if LEARNED_ALONE:
        first = re.match(r"^\W*([A-Za-z']+)", text)
        if first and first.group(1).lower() in LEARNED_ALONE:
            text = text[: first.start(1)] + "Hey Sonu" + text[first.end(1) :]
    return _called().sub(
        lambda m: f"Hey{m.group(1)}Sonu" if sounds_like_sonu(m.group(2)) else m.group(0), text
    )


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


def learn_wake(said: list[str], lookalikes: list[str]) -> tuple[set[str], set[str], set[str]]:
    """What to learn from the owner's "Hey Sonu"s (as Whisper wrote them) — names, greetings and one-word
    forms — keeping only what makes none of their look-alike phrases wake SONU. Returns (names, greetings,
    alone)."""
    names: set[str] = set()
    greetings: set[str] = set()
    alone: set[str] = set()
    for text in said:
        if after_wake_word(text) is not None:
            continue  # already heard
        words = _words(text)
        if len(words) >= 2:
            greetings.add(words[0])
            names.add(words[1])
        elif words:
            alone.add(words[0])
    # Kept one at a time, only if no look-alike phrase then wakes SONU (greetings last: they pair with names).
    kept: tuple[set[str], set[str], set[str]] = (set(), set(), set())
    for index, learned in ((0, names), (2, alone), (1, greetings)):
        for word in sorted(learned):
            kept[index].add(word)
            with _learning(*kept):
                if any(after_wake_word(text) is not None for text in lookalikes):
                    kept[index].discard(word)
    return kept


@contextlib.contextmanager
def _learning(names: set[str], greetings: set[str], alone: set[str]) -> Any:
    """Try learned words for a moment (then back as they were)."""
    saved = (set(LEARNED_NAMES), set(LEARNED_GREETINGS), set(LEARNED_ALONE))
    use(names, greetings, alone)
    try:
        yield
    finally:
        use(*saved)


def use(names: set[str], greetings: set[str], alone: set[str]) -> None:
    LEARNED_NAMES.clear()
    LEARNED_NAMES.update(names)
    LEARNED_GREETINGS.clear()
    LEARNED_GREETINGS.update(greetings)
    LEARNED_ALONE.clear()
    LEARNED_ALONE.update(alone)


def load_learned(path: Path) -> bool:
    """What `sonu voice train-wake` learned from the owner's voice (a missing or broken file: nothing)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        use(set(data.get("names", [])), set(data.get("greetings", [])), set(data.get("alone", [])))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return True


def after_wake_word(text: str) -> str | None:
    """The words after SONU's name if ``text`` starts with it ("Hey Sonu, open kitty" → "open kitty");
    None if it doesn't start with the name."""
    match = _WAKE.match(fix_name(text))
    if match is None:
        return None
    return fix_name(text)[match.end() :].strip()


@dataclass(frozen=True, slots=True)
class WakeTiming:
    silence_s: float = 0.8  # quiet this long: they're done
    max_s: float = 20.0  # never record longer


class _Segment:
    """One stretch of speech: collected until the name is checked, then kept only if it was said."""

    def __init__(self, pre_roll: list[bytes]) -> None:
        self.frames = list(pre_roll)
        self.length = len(pre_roll)  # frames so far, kept or not
        self.head: str | None = None  # what the beginning said, once checked
        self.head_length = 0  # frames the beginning had
        self.spoken_until = self.length  # frames up to the latest speech
        self.woke = False  # it started with SONU's name

    def add(self, frame: bytes) -> None:
        self.length += 1
        if self.head is None or self.woke:  # not for SONU: nothing more is kept
            self.frames.append(frame)

    def audio(self) -> bytes:
        return b"".join(self.frames)


class WakeListener:
    """Runs until cancelled; calls ``on_wake(words after the name)`` each time SONU is called."""

    def __init__(
        self,
        transcribe: Callable[[bytes, str], str],  # 16 kHz 16-bit audio, hint → text
        detector: SpeechDetector,
        on_wake: Callable[[str], Awaitable[None]],
        busy: Callable[[], bool] = lambda: False,  # SONU is working or talking: don't listen
        hint: Callable[[], str] = lambda: WAKE_HINT,  # for the whole request (app names)
        argv: Sequence[str] = RECORD_ARGV,
        timing: WakeTiming | None = None,
    ) -> None:
        self.transcribe, self.detector, self.on_wake = transcribe, detector, on_wake
        self.busy, self.hint, self.argv = busy, hint, argv
        self.timing = timing or WakeTiming()
        self.heard_frames = 0  # frames read from the microphone (the check that it's really listening)

    async def run(self) -> None:
        window: list[bytes] = []
        segment: _Segment | None = None
        async with contextlib.aclosing(frames(self.argv)) as stream:
            async for frame in stream:
                self.heard_frames += 1
                if self.busy():
                    window, segment = [], None  # SONU's own voice, or a request being heard
                    continue
                window = [*window[-(WINDOW_FRAMES - 1) :], frame]
                if segment is not None:
                    segment.add(frame)
                if self.heard_frames % DETECT_EVERY:
                    continue
                speech = await asyncio.to_thread(self.detector, b"".join(window))
                segment = await self._step(segment, window, speech)

    async def _step(
        self, segment: _Segment | None, window: list[bytes], speech: list[tuple[int, int]]
    ) -> _Segment | None:
        """Every 300 ms: start, check, or finish a stretch of speech."""
        samples = len(window) * FRAME_BYTES // 2
        quiet_s = (samples - speech[-1][1]) / RATE if speech else samples / RATE
        if segment is None:  # only speech that is still going starts one (not the end of the last)
            fresh = speech and quiet_s < 2 * DETECT_EVERY * FRAME_MS / 1000
            return _Segment(window[-(PRE_ROLL_FRAMES + DETECT_EVERY) :]) if fresh else None
        if speech:
            segment.spoken_until = max(segment.spoken_until, segment.length - int(quiet_s * 1000) // FRAME_MS)
        ended = quiet_s >= self.timing.silence_s or segment.length * FRAME_MS >= self.timing.max_s * 1000
        if segment.head is None and (ended or segment.length >= HEAD_FRAMES):
            segment.head = await asyncio.to_thread(self.transcribe, segment.audio(), WAKE_HINT)
            segment.head_length = segment.length
            segment.woke = after_wake_word(segment.head) is not None
            if not segment.woke:
                segment.frames.clear()
        if not ended:
            return segment
        if segment.woke:
            text = segment.head or ""
            if segment.spoken_until > segment.head_length:  # more was said after the part already heard
                text = await asyncio.to_thread(self.transcribe, segment.audio(), self.hint())
            words = after_wake_word(text)
            await self.on_wake(words if words is not None else after_wake_word(segment.head or "") or "")
        return None
