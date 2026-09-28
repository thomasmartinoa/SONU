"""One spoken exchange: listen to the user, and say SONU's reply out loud.

Safety (D42): a **medium**-risk step may be approved by voice ("yes" / "go ahead"); anything unclear
is a no. A **high**-risk step (merging, installing, shutting down, going live) is never approved by
voice alone: SONU says what it wants to do and the answer must come from the desktop picker (a click
or the keyboard) — a TV or another person in the room can't approve it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sonu.core.engine import ConfirmRequest
from sonu.tools.base import Risk
from sonu.voice.audio import Endpointing, Speaker, SpeechDetector, Utterance, record
from sonu.voice.engines import KokoroVoice, WhisperEars

_YES = re.compile(
    r"^\W*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|please do|confirm(ed)?|affirmative)\b", re.I
)
_NO = re.compile(r"\b(no|nope|don't|do not|stop|cancel|wait|never)\b", re.I)

Recorder = Callable[[Endpointing | None, SpeechDetector | None], Awaitable[Utterance]]


async def _microphone(endpointing: Endpointing | None, detector: SpeechDetector | None) -> Utterance:
    return await record(endpointing, detector=detector)


def said_yes(text: str) -> bool:
    """A clear yes — and nothing that takes it back ("yes… no, wait")."""
    return bool(_YES.search(text)) and not _NO.search(text)


@dataclass
class VoiceSession:
    ears: WhisperEars
    voice: KokoroVoice
    recorder: Recorder = _microphone
    detector: SpeechDetector | None = None  # None: judge by loudness
    hint: str = "Sonu, workspace"  # words likely to be said: set from the installed apps
    speaker_argv: tuple[str, ...] = ("pw-play",)
    _speaker: Speaker | None = field(default=None, init=False)

    async def hear(self, endpointing: Endpointing | None = None) -> str:
        """Record until the user stops talking; their words ("" if nothing was said)."""
        utterance = await self.recorder(endpointing, self.detector)
        if not utterance.spoke:
            return ""
        return await asyncio.to_thread(self.ears.transcribe, utterance.audio(), self.hint)

    async def say(self, text: str) -> None:
        if not text.strip():
            return
        samples = await asyncio.to_thread(self.voice.speak, text)
        self._speaker = Speaker(self.voice.rate, self.speaker_argv)
        try:
            await self._speaker.play(samples)
        finally:
            self._speaker = None

    def stop_talking(self) -> None:
        """The user pressed the key again or started talking: stop mid-sentence."""
        if self._speaker is not None:
            self._speaker.stop()

    async def confirm_by_voice(self, request: ConfirmRequest) -> bool:
        """Medium risk: ask, listen, accept only a clear yes. High risk is never voice-only."""
        if request.risk is Risk.HIGH:
            return False
        await self.say(f"Should I {request.description[0].lower()}{request.description[1:]}? Say yes or no.")
        answer = await self.hear(Endpointing(start_timeout_s=6.0, silence_s=0.7, max_s=6.0))
        return said_yes(answer)

    async def warm_up(self) -> None:
        """Load both models now, so the first press of the key doesn't wait for them."""
        await asyncio.to_thread(self.ears.load)
        await asyncio.to_thread(self.voice.load)
