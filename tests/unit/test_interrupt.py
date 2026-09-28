"""Talking over SONU: the echo canceller, telling the user from SONU's echo, and stopping (D50)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sonu.voice.echo import SINK, EchoCanceller
from sonu.voice.interrupt import is_the_user, user_talked
from sonu.voice.session import VoiceSession
from tests.unit.test_wake import loud_parts, silence, speech

SAID = "Your laptop is running cool at sixty degrees, memory use is low, and you have plenty of disk space."


@pytest.mark.parametrize(
    ("heard", "user"),
    [
        ("Stop.", True),
        ("Wait, open kitty instead.", True),
        ("That's fine.", True),  # found on the laptop: another voice over SONU's, garbled — still someone
        ("", False),  # found on the laptop: the leftover echo transcribes to nothing
        ("Thank you.", False),  # what Whisper writes for noise
        ("memory use is low and you have plenty", False),  # SONU's own sentence
        ("plenty of disk space.", False),
    ],
)
def test_is_the_user(heard: str, user: bool) -> None:
    assert is_the_user(heard, SAID) is user


async def watch(tmp_path: Path, audio: bytes, heard: str) -> tuple[bool, list[float]]:
    mic = tmp_path / "aec.raw"
    mic.write_bytes(audio)
    asked: list[float] = []

    def transcribe(pcm16: bytes, hint: str) -> str:
        asked.append(len(pcm16) / 32000)
        return heard

    return await user_talked(SAID, loud_parts, transcribe, argv=("cat", str(mic))), asked


@pytest.mark.anyio
async def test_the_user_talking_over_sonu_is_noticed(tmp_path: Path) -> None:
    talked, asked = await watch(tmp_path, silence(0.9) + speech(0.9) + silence(0.6), "Stop.")
    assert talked and len(asked) == 1


@pytest.mark.anyio
async def test_a_short_blip_is_not_transcribed(tmp_path: Path) -> None:
    # Found on the laptop: the echo canceller left a 0.38 s blip of SONU's voice.
    talked, asked = await watch(tmp_path, silence(0.9) + speech(0.3) + silence(1.5), "Stop.")
    assert not talked and asked == []


@pytest.mark.anyio
async def test_sonus_own_words_dont_stop_it(tmp_path: Path) -> None:
    talked, asked = await watch(tmp_path, silence(0.9) + speech(1.2) + silence(0.6), "memory use is low")
    assert not talked and asked


# ---------------------------------------------------------------------------- the echo canceller
class Pactl:
    def __init__(self, sinks: str = "", load: tuple[int, str] = (0, "536870916\n")) -> None:
        self.sinks, self.load, self.calls = sinks, load, []  # type: ignore[var-annotated]

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[1:3] == ["list", "short"]:
            return 0, self.sinks
        if argv[1] == "load-module":
            return self.load
        return 0, ""


def test_the_canceller_is_made_once_and_removed() -> None:
    pactl = Pactl()
    echo = EchoCanceller(run=pactl, which=lambda name: f"/usr/bin/{name}")
    assert echo.ensure()
    load = pactl.calls[1]
    assert load[:3] == ["pactl", "load-module", "module-echo-cancel"] and f"sink_name={SINK}" in load
    assert not any("set-default" in arg for call in pactl.calls for arg in call)  # the user's devices stay
    echo.close()
    assert pactl.calls[-1] == ["pactl", "unload-module", "536870916"]


def test_one_left_by_an_earlier_daemon_is_reused() -> None:
    pactl = Pactl(sinks=f"58\talsa_output.speaker\tPipeWire\n2167\t{SINK}\tPipeWire\n")
    echo = EchoCanceller(run=pactl, which=lambda name: f"/usr/bin/{name}")
    assert echo.ensure() and echo.module is None
    echo.close()
    assert all(call[1] != "unload-module" for call in pactl.calls)


def test_no_canceller_without_pactl_or_when_loading_fails() -> None:
    assert not EchoCanceller(run=Pactl(), which=lambda name: None).ensure()
    assert not EchoCanceller(run=Pactl(load=(1, "")), which=lambda name: f"/usr/bin/{name}").ensure()


# ---------------------------------------------------------------------------- say(), interrupted
class Voice:
    rate = 24000

    def speak(self, text: str) -> bytes:
        return b"\x00" * 8


def session(talks_after: float | None) -> VoiceSession:
    async def interruptions(spoken: str) -> bool:
        if talks_after is None:
            await asyncio.sleep(3600)
        await asyncio.sleep(talks_after or 0)
        return True

    # A "player" that takes 2 s to play the sentence.
    voice = VoiceSession(None, Voice(), speaker_argv=("sh", "-c", "cat >/dev/null; sleep 2"))  # type: ignore[arg-type]
    voice.interruptions = interruptions
    return voice


@pytest.mark.anyio
async def test_talking_over_sonu_stops_it() -> None:
    started = time.monotonic()
    assert await session(0.1).say(SAID, interruptible=True) is True
    assert time.monotonic() - started < 1.5


@pytest.mark.anyio
async def test_a_reply_nobody_talks_over_is_said_whole() -> None:
    started = time.monotonic()
    assert await session(None).say(SAID, interruptible=True) is False
    assert time.monotonic() - started >= 2


@pytest.mark.anyio
async def test_greetings_and_questions_are_not_interruptible() -> None:
    started = time.monotonic()
    assert await session(0.1).say("How can I help?") is False
    assert time.monotonic() - started >= 2
