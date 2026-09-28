""" "Hey Sonu": the wake phrase, the listener (a fake speech detector over audio from a file) and
the voice.wake tool."""

from __future__ import annotations

import array
from pathlib import Path

import pytest

from sonu.testing.fake_wake import FakeWake
from sonu.tools.base import Prepared, Risk, StepStatus, ToolOutcome
from sonu.tools.voice_tools import Wake, WakeArgs
from sonu.voice.audio import RATE
from sonu.voice.wake import HEAD_FRAMES, WAKE_HINT, WakeListener, after_wake_word, fix_name
from sonu.world.snapshot import WorldSnapshot


# Found on the laptop (Whisper large-v3-turbo with the "Hey Sonu" hint, three Kokoro voices).
@pytest.mark.parametrize(
    ("heard", "words"),
    [
        ("Hey Sonu", ""),
        ("Hey Sonu, open Kitty on Workspace 2.", "open Kitty on Workspace 2."),
        ("Hey Sonu OpenKitty on Workspace 2", "OpenKitty on Workspace 2"),
        ("Sonu, what's my battery?", "what's my battery?"),
        ("Hi Sonu", ""),
        ("Hey, Sanu!", ""),  # without the hint
        ("okay sonu turn it down", "turn it down"),
        ("I told Sonu about it.", None),  # the hint turns "Sonny" into "Sonu": only the start counts
        ("The song was great.", None),
        ("Sonuvabitch", None),
        ("", None),
    ],
)
def test_after_wake_word(heard: str, words: str | None) -> None:
    assert after_wake_word(heard) == words


# Found on real hardware: the owner's voice through the laptop's mic, 2026-09-28.
@pytest.mark.parametrize(
    "heard",
    [
        "Listen for Hey Sovenue",
        "Listen for Hey Soinu",
        "Listen for He Sonu",
        "Listen for Hey Soenu",
        "Listen for Hey Soin",
        "Listen for his sonu",  # `sonu voice check` with the owner's voice (large-v3-turbo)
    ],
)
def test_a_misheard_name_after_a_greeting_is_sonu(heard: str) -> None:
    assert fix_name(heard) == "Listen for Hey Sonu"


@pytest.mark.parametrize(
    "heard",
    [
        "his son is here",
        "his sound",
        "hey son, come here",
        "Hey Siri",
        "I told Sonny",
        "hi sunny",
        "he said so",
        "hey sony",
        "okay so",
    ],
)
def test_other_words_are_left_alone(heard: str) -> None:
    assert fix_name(heard) == heard


def test_the_name_written_as_one_word() -> None:
    # Found on the laptop (large-v3): "Listen for HeySanu."
    assert fix_name("Listen for HeySanu.") == "Listen for Hey Sonu."
    assert after_wake_word("HiSonu, open kitty") == "open kitty"
    assert fix_name("Heysham is a town") == "Heysham is a town"


def test_a_misheard_name_still_wakes() -> None:
    assert after_wake_word("Hey Soin, open kitty") == "open kitty"
    assert after_wake_word("He Soenu") == ""


def speech(seconds: float) -> bytes:
    return array.array("h", [6000] * int(RATE * seconds)).tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(RATE * seconds) * 2)


def loud_parts(pcm16: bytes) -> list[tuple[int, int]]:
    """A stand-in for Silero: where the samples are loud."""
    samples = array.array("h", pcm16)
    loud = [i for i, sample in enumerate(samples) if abs(sample) > 1000]
    return [(loud[0], loud[-1] + 1)] if loud else []


class Script:
    """Transcriptions in order, and what was asked."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.asked: list[tuple[float, str]] = []  # (seconds of audio, hint)

    def __call__(self, pcm16: bytes, hint: str) -> str:
        self.asked.append((len(pcm16) / 2 / RATE, hint))
        return self.texts.pop(0) if self.texts else ""


async def listen(tmp_path: Path, audio: bytes, script: Script, busy: bool = False) -> list[str]:
    source = tmp_path / "mic.raw"
    source.write_bytes(audio)
    woke: list[str] = []

    async def on_wake(words: str) -> None:
        woke.append(words)

    listener = WakeListener(
        script,
        loud_parts,
        on_wake,
        busy=lambda: busy,
        hint=lambda: "Hey Sonu, kitty",
        argv=("cat", str(source)),
    )
    await listener.run()
    assert listener.heard_frames > 0
    return woke


@pytest.mark.anyio
async def test_a_request_after_the_name(tmp_path: Path) -> None:
    script = Script("Hey Sonu, open kitty on 2")
    woke = await listen(tmp_path, silence(0.6) + speech(1.2) + silence(1.5), script)
    assert woke == ["open kitty on 2"]
    assert [hint for _, hint in script.asked] == [WAKE_HINT]  # short: the first look was all of it


@pytest.mark.anyio
async def test_the_name_alone_wakes_with_no_words(tmp_path: Path) -> None:
    woke = await listen(tmp_path, silence(0.6) + speech(0.6) + silence(1.5), Script("Hey Sonu."))
    assert woke == [""]


@pytest.mark.anyio
async def test_other_talk_is_looked_at_once_then_dropped(tmp_path: Path) -> None:
    script = Script("I told Sonu about it and then we went to the shop", "never asked")
    woke = await listen(tmp_path, silence(0.6) + speech(6.0) + silence(1.5), script)
    assert woke == []
    assert len(script.asked) == 1  # the rest of what they said was never transcribed
    seconds, _ = script.asked[0]
    assert seconds <= (HEAD_FRAMES + 30) * 0.03  # only the beginning


@pytest.mark.anyio
async def test_a_long_request_is_heard_whole_with_the_app_names(tmp_path: Path) -> None:
    script = Script("Hey Sonu, open", "Hey Sonu, open kitty on workspace 2 and play some music")
    woke = await listen(tmp_path, silence(0.6) + speech(4.0) + silence(1.5), script)
    assert woke == ["open kitty on workspace 2 and play some music"]
    assert [hint for _, hint in script.asked] == [WAKE_HINT, "Hey Sonu, kitty"]
    assert script.asked[1][0] >= 4.0  # the whole request, from just before it started


@pytest.mark.anyio
async def test_nothing_is_heard_while_sonu_is_busy(tmp_path: Path) -> None:
    script = Script("Hey Sonu, open kitty")
    assert await listen(tmp_path, silence(0.6) + speech(1.2) + silence(1.5), script, busy=True) == []
    assert script.asked == []


@pytest.mark.anyio
async def test_two_requests_in_a_row(tmp_path: Path) -> None:
    audio = silence(0.6) + speech(1.0) + silence(1.5) + speech(1.0) + silence(1.5)
    assert await listen(tmp_path, audio, Script("Sonu, pause", "Sonu, next song")) == ["pause", "next song"]


# ---------------------------------------------------------------------------- voice.wake
async def prepare(on: bool, wake: FakeWake | None) -> Prepared | ToolOutcome:
    from sonu.tools.context import ToolContext

    ctx = ToolContext.__new__(ToolContext)
    ctx.wake = wake
    return await Wake().prepare(WakeArgs(on=on), ctx, WorldSnapshot(windows=()))


@pytest.mark.anyio
async def test_turning_it_on_asks_first_and_is_checked() -> None:
    wake = FakeWake()
    prepared = await prepare(True, wake)
    assert isinstance(prepared, Prepared) and prepared.risk is Risk.MEDIUM
    assert "microphone stays on" in prepared.description
    from sonu.tools.context import ToolContext

    ctx = ToolContext.__new__(ToolContext)
    ctx.wake = wake
    outcome = await Wake().execute(prepared, ctx)
    assert outcome.status is StepStatus.DONE and wake.listening
    assert "stop listening for Hey Sonu" in outcome.summary


@pytest.mark.anyio
async def test_turning_it_off_doesnt_ask() -> None:
    prepared = await prepare(False, FakeWake(listening=True))
    assert isinstance(prepared, Prepared) and prepared.risk is None


@pytest.mark.anyio
async def test_already_in_that_state() -> None:
    outcome = await prepare(True, FakeWake(listening=True))
    assert isinstance(outcome, ToolOutcome) and outcome.status is StepStatus.DONE
    assert outcome.summary == "Already listening for 'Hey Sonu'"


@pytest.mark.anyio
async def test_without_the_daemon_it_says_so() -> None:
    outcome = await prepare(True, None)
    assert isinstance(outcome, ToolOutcome) and outcome.status is StepStatus.FAILED
    assert "sonu daemon install" in outcome.summary


@pytest.mark.anyio
async def test_a_microphone_that_doesnt_start_is_a_failure() -> None:
    from sonu.tools.context import ToolContext

    wake = FakeWake(problem="the microphone didn't start (pw-record)")
    ctx = ToolContext.__new__(ToolContext)
    ctx.wake = wake
    outcome = await Wake().execute(Prepared("…", True), ctx)
    assert (
        outcome.status is StepStatus.FAILED and outcome.summary == "the microphone didn't start (pw-record)"
    )
