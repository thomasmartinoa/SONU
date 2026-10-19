from __future__ import annotations

import array
import math

import pytest

from sonu.core.engine import ConfirmRequest
from sonu.tools.base import Risk
from sonu.voice.audio import FRAME_BYTES, RECORD_ARGV, AudioError, Endpointing, Speaker, Utterance, record
from sonu.voice.session import said_yes


def tone(level: float, frames: int) -> list[bytes]:
    """30 ms frames of a 440 Hz tone at an RMS of about ``level`` (0 = silence)."""
    samples_per_frame = FRAME_BYTES // 2
    out = []
    for f in range(frames):
        samples = array.array(
            "h",
            (
                int(level * math.sqrt(2) * math.sin(2 * math.pi * 440 * (f * samples_per_frame + i) / 16000))
                for i in range(samples_per_frame)
            ),
        )
        out.append(samples.tobytes())
    return out


def feed(utterance: Utterance, frames: list[bytes]) -> int:
    """Feed frames until the utterance ends; how many were taken."""
    for index, frame in enumerate(frames):
        if not utterance.feed(frame):
            return index + 1
    return len(frames)


# ---------------------------------------------------------------- end of speech
def test_it_stops_after_the_user_stops_talking() -> None:
    utterance = Utterance(Endpointing(silence_s=0.6))
    taken = feed(utterance, tone(50, 10) + tone(3000, 40) + tone(50, 100))
    assert utterance.spoke
    assert taken == 10 + 40 + 20  # 0.6 s of quiet = 20 frames of 30 ms


def test_nothing_said_ends_quietly() -> None:
    utterance = Utterance(Endpointing(start_timeout_s=1.5))
    assert feed(utterance, tone(50, 200)) == 50  # 1.5 s
    assert not utterance.spoke


def test_a_noisy_room_raises_the_bar() -> None:
    utterance = Utterance()
    feed(utterance, tone(900, 10))  # a fan, measured in the first 300 ms
    assert utterance.threshold == pytest.approx(2700, rel=0.05)


# ---------------------------------------------------------------- the programs
def test_raw_audio_both_ways() -> None:
    # Found on the laptop: without --raw, pw-record wrote an AU header and pw-play refused raw samples.
    assert "--raw" in RECORD_ARGV
    assert "--raw" in Speaker(24000).argv


@pytest.mark.anyio
async def test_a_player_that_fails_is_reported() -> None:
    with pytest.raises(AudioError, match="couldn't play"):
        await Speaker(24000, ("false",)).play(b"\x00" * 16)


@pytest.mark.anyio
async def test_recording_from_a_program() -> None:
    # `cat` of a file stands in for pw-record: 0.3 s of quiet, speech, then silence.
    import tempfile

    with tempfile.NamedTemporaryFile() as audio:
        audio.write(b"".join(tone(50, 10) + tone(3000, 20) + tone(50, 60)))
        audio.flush()
        utterance = await record(Endpointing(silence_s=0.6), ("cat", audio.name))
    assert utterance.spoke and len(utterance.frames) == 10 + 20 + 20


# ---------------------------------------------------------------- spoken yes/no
@pytest.mark.parametrize(
    ("text", "yes"),
    [
        ("Yes.", True),
        ("Yeah, go ahead.", True),
        ("Okay, do it", True),
        ("No.", False),
        ("Yes... no, wait.", False),
        ("What?", False),
        ("", False),
    ],
)
def test_only_a_clear_yes_counts(text: str, yes: bool) -> None:
    assert said_yes(text) is yes


@pytest.mark.anyio
async def test_high_risk_is_never_approved_by_voice_alone() -> None:
    from sonu.voice.session import VoiceSession

    session = VoiceSession.__new__(VoiceSession)  # no models needed: it must refuse before listening
    request = ConfirmRequest("Shut down the laptop", "system.power", Risk.HIGH, "high-risk action")
    assert await session.confirm_by_voice(request) is False


def test_whisper_loads_the_copy_on_disk_without_asking_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found on real hardware: after a reinstall, Whisper's loading asked the Hugging Face Hub about a model
    already on disk, the connection hung, and sonud answered no request at all."""
    import sys
    import types

    from sonu.voice import engines

    loads: list[dict[str, object]] = []
    on_disk = {"large-v3-turbo"}

    class WhisperModel:
        def __init__(self, name: str, **kwargs: object) -> None:
            loads.append({"name": name, **kwargs})
            if kwargs.get("local_files_only") and name not in on_disk:
                raise FileNotFoundError(name)  # huggingface_hub's LocalEntryNotFoundError is one

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setattr(engines, "voice_installed", lambda: True)
    monkeypatch.setattr(engines, "_preload_cuda_libraries", lambda: None)

    engines.WhisperEars("large-v3-turbo").load()
    assert [load.get("local_files_only") for load in loads] == [True]  # the Hub isn't asked
    loads.clear()
    engines.WhisperEars("large-v3").load()  # not downloaded yet: from the Hub, as before
    assert [load.get("local_files_only") for load in loads] == [True, None]
