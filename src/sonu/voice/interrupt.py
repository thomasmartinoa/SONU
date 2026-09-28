"""Talking over SONU (barge-in): notice the user speaking while SONU talks, so it can stop (D50).

The microphone is the echo canceller's source (``sonu.voice.echo``), so SONU's own voice is mostly
gone. What's left is checked twice before SONU stops: at least 0.4 s of speech (the leftover echo
was a 0.38 s blip on the laptop), and its words must be something — and not SONU's own sentence
(Whisper also writes "Thank you." for noise). The words themselves aren't used as a request: over
SONU's voice they come out garbled; SONU stops and listens instead, like a person interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence

from sonu.voice.audio import DETECT_EVERY, RATE, SpeechDetector, frames
from sonu.voice.echo import SOURCE

MIN_SPEECH_S = 0.4
WINDOW_FRAMES = 50  # the detector looks at the last 1.5 s
_NOISE = {"thank you", "thanks for watching", "you", "bye", "okay", "so", "uh", "um", "hmm"}
LISTEN_ARGV = (
    "pw-record",
    "--target",
    SOURCE,
    "--raw",
    "--rate",
    str(RATE),
    "--channels",
    "1",
    "--format",
    "s16",
    "-",
)


def _words(text: str) -> list[str]:
    return "".join(c.lower() if c.isalnum() or c == "'" else " " for c in text).split()


def is_the_user(heard: str, spoken: str) -> bool:
    """Words from the microphone while SONU said ``spoken``: the user's, or SONU's echo / noise?"""
    words = _words(heard)
    if not words or " ".join(words) in _NOISE:
        return False
    own = set(_words(spoken))
    return sum(word in own for word in words) / len(words) < 0.6


async def user_talked(
    spoken: str,
    detector: SpeechDetector,
    transcribe: Callable[[bytes, str], str],
    argv: Sequence[str] = LISTEN_ARGV,
) -> bool:
    """Listen while SONU says ``spoken``; returns True as soon as the user talks over it (runs until
    cancelled otherwise)."""
    window: list[bytes] = []
    count = 0
    async with contextlib.aclosing(frames(argv)) as stream:
        async for frame in stream:
            window = [*window[-(WINDOW_FRAMES - 1) :], frame]
            count += 1
            if count % DETECT_EVERY:
                continue
            audio = b"".join(window)
            speech = await asyncio.to_thread(detector, audio)
            longest = max((end - start for start, end in speech), default=0)
            if longest < MIN_SPEECH_S * RATE:
                continue
            start = max(0, speech[-1][0] - RATE // 10) * 2  # bytes: 16-bit samples
            heard = await asyncio.to_thread(transcribe, audio[start:], "")
            if is_the_user(heard, spoken):
                return True
    return False
