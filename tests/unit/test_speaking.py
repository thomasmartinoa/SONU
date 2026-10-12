"""Saying a reply sooner (D85): sentence by sentence, the next made while the one before plays."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest

from sonu.voice import session as session_module
from sonu.voice.audio import Speaker
from sonu.voice.session import VoiceSession, sentences

REPLY = "You're at 80 percent. Your fans are loud because Ollama is busy. Want me to stop it?"


def test_sentences() -> None:
    assert sentences(REPLY) == [
        "You're at 80 percent.",
        "Your fans are loud because Ollama is busy.",
        "Want me to stop it?",
    ]
    assert sentences("Use e.g. kitty. Version 3.5 is out.") == ["Use e.g. kitty.", "Version 3.5 is out."]
    assert sentences("Done.") == ["Done."]


class Voice:
    rate = 24000

    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events

    def speak(self, text: str) -> bytes:
        time.sleep(0.05)  # making the sound takes a moment
        self.events.append(("made", text))
        return text.encode()


class FakeSpeaker:
    """Records what reaches the player, and when."""

    events: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, rate: int, argv: tuple[str, ...]) -> None:
        self._stopped = False

    async def play(self, samples: bytes | AsyncIterator[bytes]) -> None:
        self.events.append(("start", ""))
        if isinstance(samples, bytes):
            self.events.append(("played", samples.decode()))
        else:
            async for piece in samples:
                self.events.append(("played", piece.decode()))
                await asyncio.sleep(0.1)  # it plays for a while
        self.events.append(("end", ""))

    def stop(self) -> None:
        self._stopped = True


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    log: list[tuple[str, str]] = []
    monkeypatch.setattr(FakeSpeaker, "events", log)
    monkeypatch.setattr(session_module, "Speaker", FakeSpeaker)
    return log


@pytest.mark.anyio
async def test_the_first_sentence_plays_while_the_next_is_made(events: list[tuple[str, str]]) -> None:
    voice = VoiceSession(None, Voice(events))  # type: ignore[arg-type]
    await voice.say(REPLY)
    first, second, third = sentences(REPLY)
    played = [text for kind, text in events if kind == "played"]
    assert played == [first, second, third]  # the old code: the whole reply, made first, in one piece
    assert events.index(("played", first)) < events.index(("made", second))
    assert events.index(("played", second)) < events.index(("made", third))


@pytest.mark.anyio
async def test_one_thing_is_said_at_a_time(events: list[tuple[str, str]]) -> None:
    voice = VoiceSession(None, Voice(events))  # type: ignore[arg-type]
    await asyncio.gather(voice.say("One moment."), voice.say(REPLY))
    kinds = [kind for kind, _ in events if kind in ("start", "end")]
    assert kinds == ["start", "end", "start", "end"]  # never two voices over each other


@pytest.mark.anyio
async def test_the_player_takes_pieces_and_stops_between_them(tmp_path: Path) -> None:
    out = tmp_path / "played"
    gate = asyncio.Event()

    async def pieces() -> AsyncIterator[bytes]:
        yield b"one."
        await gate.wait()
        yield b"two."

    gate.set()
    speaker = Speaker(24000, ("sh", "-c", f"cat > {out}"))
    await speaker.play(pieces())
    assert out.read_bytes() == b"one.two."

    speaker = Speaker(24000, ("sh", "-c", f"cat > {out}"))
    gate.clear()
    playing = asyncio.create_task(speaker.play(pieces()))
    await asyncio.sleep(0.2)
    speaker.stop()  # talked over after the first sentence
    gate.set()
    await asyncio.wait_for(playing, 3)
    assert b"two." not in out.read_bytes()
