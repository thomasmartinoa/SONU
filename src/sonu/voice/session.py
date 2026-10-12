"""One spoken exchange: listen to the user, and say SONU's reply out loud.

Safety (D42): a **medium**-risk step may be approved by voice ("yes" / "go ahead"); anything unclear
is a no. A **high**-risk step (merging, installing, shutting down, going live) is never approved by
voice alone: SONU says what it wants to do and the answer must come from the desktop picker (a click
or the keyboard) — a TV or another person in the room can't approve it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from sonu.core.engine import ConfirmRequest
from sonu.tools.base import Risk
from sonu.voice.audio import Endpointing, Speaker, SpeechDetector, Utterance, record
from sonu.voice.engines import KokoroVoice, WhisperEars
from sonu.voice.speech import for_speech

_YES = re.compile(
    r"^\W*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|please do|confirm(ed)?|affirmative)\b", re.I
)
_NO = re.compile(r"\b(no|nope|don't|do not|stop|cancel|wait|never)\b", re.I)

# A sentence ends: ". " / "! " / "? " before a capital, a digit or a quote (not "e.g. so" or "3.5").
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")

Recorder = Callable[[Endpointing | None, SpeechDetector | None], Awaitable[Utterance]]

# Said when the SONU key is pressed, before listening — taking turns, so it doesn't sound canned.
# "{name}": the user's name, when SONU knows it (lines with it are skipped otherwise).
GREETINGS = ("How can I help?", "Yes, {name}?", "What can I do for you?", "I'm listening.", "Yes?")


async def _microphone(endpointing: Endpointing | None, detector: SpeechDetector | None) -> Utterance:
    return await record(endpointing, detector=detector)


def sentences(text: str) -> list[str]:
    """A reply in the pieces it's said in: the first can play while the next is still being made."""
    return [part.strip() for part in _SENTENCE_END.split(text) if part.strip()] or [text]


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
    # While SONU says something: resolves True if the user talks over it (None: no talking over).
    interruptions: Callable[[str], Coroutine[Any, Any, bool]] | None = None
    _speaker: Speaker | None = field(default=None, init=False)
    _spoken: dict[str, bytes] = field(default_factory=dict, init=False)  # greetings, ready to play
    _greeted: int = field(default=0, init=False)
    # One thing said at a time: "one moment" while a step runs, then a question or the reply, in turn.
    _talking: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # Told when SONU starts and stops talking (the HUD stays up until it's done — found on the laptop).
    speaking: Callable[[bool], None] | None = None
    # Who hears the user's requests (D96): a cloud service, when chosen and allowed. None: the local ears.
    # "Hey Sonu", talking over SONU and a spoken password always use the local ears.
    request_ears: Any = None
    # SONU's voice from the cloud (D98), when chosen and allowed. None: Kokoro. say(local=True) always Kokoro.
    cloud_voice: Any = None

    async def hear(
        self, endpointing: Endpointing | None = None, *, hint: str | None = None, local: bool = False
    ) -> str:
        """Record until the user stops talking; their words ("" if nothing was said). ``hint``: instead
        of the usual one ("" for a password, so app names can't bend it). ``local``: never the cloud."""
        utterance = await self.recorder(endpointing, self.detector)
        if not utterance.spoke:
            return ""
        ears = self.ears if local or self.request_ears is None else self.request_ears
        return await asyncio.to_thread(
            ears.transcribe, utterance.audio(), self.hint if hint is None else hint
        )

    async def hear_secret(self, prompt: str) -> str | None:
        """Ask for a password and hear it (D57): nothing but the caller ever gets the words — not the
        screen, the audit log, the conversation or a model. The period Whisper ends a sentence with and the
        spaces between spoken parts are dropped."""
        await self.say(prompt)
        # a password is never sent anywhere — only this laptop hears it (D96)
        heard = await self.hear(
            Endpointing(start_timeout_s=10.0, silence_s=1.0, max_s=15.0), hint="", local=True
        )
        secret = (
            heard.strip().removesuffix(".").replace(" ", "")
        )  # Whisper's sentence period; "!" may be real
        return secret or None

    def greeting(self, name: str | None = None) -> str:
        """The next greeting, in turn."""
        lines = [line.format(name=name) for line in GREETINGS if name or "{name}" not in line]
        line = lines[self._greeted % len(lines)]
        self._greeted += 1
        return line

    async def greet(self, name: str | None = None) -> None:
        """Say hello when the key is pressed — the cue to start talking (listening starts after it)."""
        await self.say(self.greeting(name), keep=True)

    async def say(
        self, text: str, *, keep: bool = False, interruptible: bool = False, local: bool = False
    ) -> bool:
        """Say ``text``; ``keep``: remember the sound, so saying it again starts at once.
        ``interruptible``: stop if the user talks over it — returns True then. ``local``: Kokoro, never the
        cloud voice (a reply holding something private, D98)."""
        if not text.strip():
            return False
        voice = self.voice if local or self.cloud_voice is None else self.cloud_voice
        async with self._talking:
            if self.speaking is not None:
                self.speaking(True)
            try:
                return await self._say(text, voice, keep=keep, interruptible=interruptible)
            finally:
                if self.speaking is not None:
                    self.speaking(False)

    async def _say(self, text: str, voice: Any, *, keep: bool, interruptible: bool) -> bool:
        text = for_speech(text)  # paths, links and times the way a person says them
        cached = voice is (self.cloud_voice or self.voice)  # the greetings were made in this voice
        found = self._spoken.get(text) if cached else None
        # Said sentence by sentence (D85): the first starts as soon as it's made — not after the whole
        # reply (measured on the laptop: 0.6 s instead of 1.25 s for two sentences, more for longer ones).
        parts = [text] if found is not None or keep else sentences(text)
        samples: bytes = found if found is not None else await asyncio.to_thread(voice.speak, parts[0])
        if found is None and keep and cached:
            self._spoken[text] = samples
        speaker = self._speaker = Speaker(voice.rate, self.speaker_argv)
        speech = samples if len(parts) == 1 else self._pieces(voice, samples, parts[1:])
        playing = asyncio.create_task(speaker.play(speech))
        watching = (
            asyncio.create_task(self.interruptions(text)) if interruptible and self.interruptions else None
        )
        try:
            if watching is not None:
                await asyncio.wait({playing, watching}, return_when=asyncio.FIRST_COMPLETED)
                talked = watching.done() and not watching.cancelled() and watching.exception() is None
                if talked and watching.result():
                    speaker.stop()
                    await playing
                    return True
            await playing
            return False
        finally:
            if watching is not None and not watching.done():
                watching.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watching
            self._speaker = None

    async def _pieces(self, voice: Any, first: bytes, rest: list[str]) -> AsyncIterator[bytes]:
        """The first sentence's sound, then each next one — made while the one before plays."""
        making = asyncio.ensure_future(asyncio.to_thread(voice.speak, rest[0]))
        try:
            yield first
            for index in range(len(rest)):
                samples = await making
                if index + 1 < len(rest):
                    making = asyncio.ensure_future(asyncio.to_thread(voice.speak, rest[index + 1]))
                yield samples
        finally:
            making.cancel()  # talked over: the rest isn't said (a sentence being made just finishes)

    def stop_talking(self) -> None:
        """The user pressed the key again or started talking: stop mid-sentence."""
        if self._speaker is not None:
            self._speaker.stop()

    async def confirm_by_voice(self, request: ConfirmRequest) -> bool:
        """Medium risk: ask, listen, accept only a clear yes. High risk is never voice-only."""
        if request.risk is Risk.HIGH:
            return False
        asked = request.spoken or request.description
        await self.say(f"Should I {asked[0].lower()}{asked[1:]}? Say yes or no.")
        answer = await self.hear(Endpointing(start_timeout_s=6.0, silence_s=0.7, max_s=6.0))
        return said_yes(answer)

    async def warm_up(self, name: str | None = None) -> None:
        """Load both models and prepare the greetings now, so pressing the key answers at once."""
        await asyncio.to_thread(self.ears.load)
        await asyncio.to_thread(self.voice.load)
        for line in GREETINGS:
            if name or "{name}" not in line:
                text = line.format(name=name)
                self._spoken[text] = await asyncio.to_thread((self.cloud_voice or self.voice).speak, text)
