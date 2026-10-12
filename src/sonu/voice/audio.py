"""The microphone and the speaker, through PipeWire's own programs (``pw-record``, ``pw-play``).

* Recording is 16 kHz mono 16-bit — what speech recognition wants — read from ``pw-record``'s
  stdout in 30 ms frames. It stops by itself once the user has spoken and then been quiet for a
  moment (end of speech), or after a hard limit; before speech starts it waits a few seconds, so a
  press of the SONU key with nothing said ends quietly.
* End of speech: a speech detector (Silero VAD, which comes with faster-whisper) looks at the audio
  every 300 ms. Found on the laptop: judging by loudness alone cut sentences short — the built-in
  mic's noise floor is high, so soft syllables counted as silence. Loudness against the room's
  noise floor stays as the fallback when no detector is given.
* Playback streams the voice's samples into ``pw-play``, so SONU starts talking before the whole
  sentence is written out, and can be stopped (the user talking over it) by ending that process.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import math
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass

RATE = 16_000
FRAME_MS = 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 16-bit mono
DETECT_EVERY = 10  # frames: the speech detector looks every 300 ms

# 16 kHz 16-bit audio → where the speech is, as (start, end) sample positions.
SpeechDetector = Callable[[bytes], list[tuple[int, int]]]


@dataclass(frozen=True, slots=True)
class Endpointing:
    """When a recording ends."""

    start_timeout_s: float = 5.0  # nothing said this long after the key: stop
    silence_s: float = 0.8  # quiet this long after speaking: the user is done
    max_s: float = 20.0  # never record longer
    calibrate_ms: int = 300  # the first frames measure the room's noise floor
    ratio: float = 3.0  # speech is this many times louder than the noise floor
    min_level: float = 300.0  # …and at least this loud (int16 RMS), for a very quiet room


def rms(frame: bytes) -> float:
    samples = array.array("h", frame[: len(frame) - len(frame) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Utterance:
    """Decides, frame by frame, whether the user is still talking."""

    def __init__(
        self, endpointing: Endpointing | None = None, detector: SpeechDetector | None = None
    ) -> None:
        self.e = endpointing or Endpointing()
        self.detector = detector
        self.frames: list[bytes] = []
        self.noise: list[float] = []
        self.spoke = False
        self.quiet_ms = 0
        self.elapsed_ms = 0

    @property
    def threshold(self) -> float:
        floor = sorted(self.noise)[len(self.noise) // 2] if self.noise else 0.0
        return max(floor * self.e.ratio, self.e.min_level)

    def feed(self, frame: bytes) -> bool:
        """Add a frame; False once the recording should stop."""
        self.elapsed_ms += FRAME_MS
        if self.detector is not None:
            return self._feed_detected(frame)
        level = rms(frame)
        if self.elapsed_ms <= self.e.calibrate_ms:
            self.noise.append(level)
            self.frames.append(frame)
            return True
        self.frames.append(frame)
        if level >= self.threshold:
            self.spoke, self.quiet_ms = True, 0
        else:
            self.quiet_ms += FRAME_MS
        if self.elapsed_ms >= self.e.max_s * 1000:
            return False
        if not self.spoke:
            return self.elapsed_ms < self.e.start_timeout_s * 1000
        return self.quiet_ms < self.e.silence_s * 1000

    def _feed_detected(self, frame: bytes) -> bool:
        self.frames.append(frame)
        if len(self.frames) % DETECT_EVERY == 0 and self.detector is not None:
            speech = self.detector(self.audio())
            if speech:
                self.spoke = True
                self.quiet_ms = (len(self.frames) * FRAME_BYTES // 2 - speech[-1][1]) * 1000 // RATE
        if self.elapsed_ms >= self.e.max_s * 1000:
            return False
        if not self.spoke:
            return self.elapsed_ms < self.e.start_timeout_s * 1000
        return self.quiet_ms < self.e.silence_s * 1000

    def audio(self) -> bytes:
        return b"".join(self.frames)


class AudioError(Exception):
    """The microphone or the speaker can't be used."""


async def frames(argv: Sequence[str]) -> AsyncGenerator[bytes, None]:
    """30 ms frames of 16 kHz mono PCM from a recorder program (``pw-record … -``).

    The pipe is SONU's own and is closed when the recording stops — found on CI (Python 3.13): a pipe
    left to the garbage collector after the recorder was stopped surfaced as an "unclosed transport".
    """
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=write_fd, stderr=asyncio.subprocess.DEVNULL
        )
    finally:
        os.close(write_fd)  # only the recorder writes
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0)
    )
    try:
        while True:
            try:
                frame = await reader.readexactly(FRAME_BYTES)
            except asyncio.IncompleteReadError:
                return
            yield frame
    finally:
        transport.close()
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        await process.wait()


# --raw: found on the laptop — without it pw-record wraps the audio in an AU container and pw-play
# rejects headerless samples ("Format not recognised").
RECORD_ARGV = ("pw-record", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-")


async def record(
    endpointing: Endpointing | None = None,
    argv: Sequence[str] = RECORD_ARGV,
    detector: SpeechDetector | None = None,
) -> Utterance:
    """Record one utterance from the default microphone; returns it (``.spoke`` False: nothing said)."""
    utterance = Utterance(endpointing, detector)
    # aclosing: stopping early must end the recorder now, not whenever the generator is collected.
    async with contextlib.aclosing(frames(argv)) as stream:
        async for frame in stream:
            if not utterance.feed(frame):
                break
    return utterance


class Speaker:
    """Plays float32 mono samples at ``rate`` through ``pw-play``; ``stop()`` ends it early."""

    def __init__(self, rate: int, argv_prefix: Sequence[str] = ("pw-play",)) -> None:
        self.rate = rate
        self.argv = (*argv_prefix, "--raw", "--rate", str(rate), "--channels", "1", "--format", "f32", "-")
        self._process: asyncio.subprocess.Process | None = None
        self._stopped = False

    async def play(self, samples: bytes | AsyncIterator[bytes]) -> None:
        """Play ``samples`` — or each piece as it comes (a reply said sentence by sentence, into one
        player: no gap but the voice's own)."""
        self._process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if self._process.stdin is None:
            raise AudioError("the player takes no input")
        try:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                if isinstance(samples, bytes):
                    self._process.stdin.write(samples)
                    await self._process.stdin.drain()
                else:
                    async with contextlib.aclosing(samples):  # type: ignore[type-var]
                        async for piece in samples:
                            if self._stopped:
                                break
                            self._process.stdin.write(piece)
                            await self._process.stdin.drain()
        finally:
            self._process.stdin.close()  # also when SONU was talked over: no pipe left behind
        code = await self._process.wait()
        stopped = self._stopped
        self._process, self._stopped = None, False
        if code != 0 and not stopped:  # never pretend SONU spoke
            raise AudioError(f"{self.argv[0]} couldn't play the voice (exit code {code})")

    def stop(self) -> None:
        self._stopped = True
        if self._process is not None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
