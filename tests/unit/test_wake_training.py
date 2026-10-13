"""Training "Hey Sonu" on the owner's voice (D100): learn how Whisper writes their "Sonu", never a word that
would wake SONU on a look-alike phrase."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.voice.wake import WAKE_HINT, after_wake_word, fix_name, learn_wake, load_learned, train, use

SAID = [
    "Hey Sonu.",
    "Hey Sonny, open kitty.",
    "A Sonu, what's my battery?",
    "Hasonu.",
    "Hey Sunu, play music",
]
LOOKALIKES = [
    "Hey Sunny, how are you?",
    "I told Sonny about it.",
    "Hey son, come here.",
    "What song is this?",
]


@pytest.fixture(autouse=True)
def nothing_learned() -> Iterator[None]:
    use(set(), set(), set())
    yield
    use(set(), set(), set())  # the learned words are module-wide: never leak into other tests


def test_learns_how_your_sonu_is_heard() -> None:
    assert [after_wake_word(t) is not None for t in SAID] == [True, False, False, False, True]
    names, greetings, alone = learn_wake(SAID, LOOKALIKES)
    assert names == {"sonny", "sonu"} and greetings == {"a", "hey"} and alone == {"hasonu"}
    use(names, greetings, alone)
    assert all(after_wake_word(t) is not None for t in SAID)
    assert not any(after_wake_word(t) is not None for t in LOOKALIKES)
    assert after_wake_word("Hey Sonny, open kitty.") == "open kitty."
    assert fix_name("A Sonu, what's my battery?").startswith("Hey Sonu")


def test_never_a_word_that_wakes_on_a_lookalike() -> None:
    """If your "Sonu" sounds like a look-alike you also said, that form isn't learned."""
    names, greetings, alone = learn_wake(["Hey Sunny."], ["Hey Sunny, how are you?"])
    use(names, greetings, alone)
    assert after_wake_word("Hey Sunny, how are you?") is None


def test_sonud_loads_what_was_learned(tmp_path: Path) -> None:
    path = tmp_path / "wake-learned.json"
    path.write_text(json.dumps({"names": ["sonny"], "greetings": ["a"], "alone": []}))
    assert load_learned(path)
    assert after_wake_word("A Sonny, open zen") == "open zen"
    assert not load_learned(tmp_path / "missing.json")


def test_the_command_learns_and_saves(
    xdg_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sonu voice train-wake` end to end, the microphone and Whisper stood in for; nothing but the words is
    kept, and the voice folder is made when it isn't there yet."""
    import sonu.cli.voice_cmd as voice_cmd
    import sonu.voice.audio as audio
    import sonu.voice.engines as engines
    from sonu.cli.app import app
    from sonu.config.paths import SonuPaths

    # each sample is heard twice: with the hint, then without it (the second listen)
    said = [("Hey Sonu.", "Hey, Sanu.")] * 6 + [("Hey Sonny, open kitty.", "Hey Sonny, open kitty.")] * 6
    said += [("A Sonu, play music.", "A Sonu, play music.")] * 6
    heard = iter(
        [text for sample in said + [("Hey son, come here.", "Hey son, come here.")] * 12 for text in sample]
    )

    class Ears:
        def __init__(self, *args: object) -> None: ...

        def transcribe(self, audio: bytes, hint: str) -> str:
            return next(heard)

    class Spoken:
        spoke = True

        def audio(self) -> bytes:
            return b"\x00" * 960

    async def record(*args: object, **kwargs: object) -> Spoken:
        return Spoken()

    monkeypatch.setattr(audio, "record", record)
    monkeypatch.setattr(engines, "WhisperEars", Ears)
    monkeypatch.setattr(engines, "silero_detector", lambda: None)
    monkeypatch.setattr(voice_cmd, "voice_installed", lambda: True)
    (tmp_path / "run").mkdir(exist_ok=True)
    env = xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}

    result = CliRunner().invoke(app, ["voice", "train-wake"], env=env)
    assert result.exit_code == 0, result.output
    assert "6/18 heard before, 18/18 now" in result.output and "0/12 now" in result.output
    saved = SonuPaths.from_env(env).data_dir / "voice" / "wake-learned.json"
    assert json.loads(saved.read_text()) == {
        "names": ["sonny", "sonu"],
        "greetings": ["a", "hey"],
        "alone": [],
        "second_listen": False,  # no look-alike woke it: nothing to gain
    }


def test_words_whisper_made_up_are_never_learned() -> None:
    """Found on real hardware: the owner's training learned "you" and "okay" as the whole name and "when"
    after "hey" / "so" — every "Okay, …" would have woken SONU."""
    names, greetings, alone = learn_wake(["You", "Okay.", "So when?", "Hey, when.", "you"], LOOKALIKES)
    assert (names, greetings, alone) == (set(), set(), set())
    use(names, greetings, alone)
    assert after_wake_word("Okay, let's go") is None and after_wake_word("You know what") is None


def test_the_head_starts_where_the_speech_does() -> None:
    """Found on real hardware: training transcribed the recording's first ~2 s — the silence while the owner
    read the line — instead of the start of what they said."""
    from sonu.voice.audio import FRAME_BYTES, RATE
    from sonu.voice.wake import HEAD_FRAMES, PRE_ROLL_FRAMES, wake_head

    silence, speech = b"\x00" * (RATE * 2 * 2), b"\x01" * (RATE * 2 * 3)  # 2 s quiet, then 3 s of talk
    audio = silence + speech
    head = wake_head(audio, lambda _: [(RATE * 2, RATE * 5)])  # the detector: speech from 2 s
    assert len(head) == HEAD_FRAMES * FRAME_BYTES
    assert head[: PRE_ROLL_FRAMES * FRAME_BYTES] == b"\x00" * (PRE_ROLL_FRAMES * FRAME_BYTES)  # 0.5 s before
    assert head[PRE_ROLL_FRAMES * FRAME_BYTES :] == b"\x01" * ((HEAD_FRAMES - PRE_ROLL_FRAMES) * FRAME_BYTES)


# The owner's training run (10-13), as Whisper wrote it with the hint; without it, what Whisper wrote for
# the same kind of audio in Kokoro's voices.
OWNER_SAID = (
    [("Hey Sonu", "Hey, Sanu.")] * 16
    + [("Okay, Sonu", "Okay, Sanu.")]
    + [("Okay, so now open", "Okay, so now open.")]
)
OWNER_LOOKALIKES = [
    ("Hey Sonu", "Hey Sunny, how are you?"),
    ("I told Sonu about", "I told Sonny about it."),
    ("Hey Son, come here", "Hey son, come here."),
    ("Hey Sonu", "Hey, so new."),
    ("Hey, Sonu Airby", "Hey, soon I'll be done."),
    ("Open the window", "Open the window."),
]


def test_listening_twice_when_it_keeps_all_of_yours() -> None:
    """Found on real hardware: with the hint, Whisper wrote "Hey Sunny", "Hey, so new" and "Hey, soon I'll be
    done" as "Hey Sonu" — 3 of the owner's 12 look-alikes woke SONU."""
    found = train(OWNER_SAID, OWNER_LOOKALIKES)
    assert found.second_listen
    assert found.before == (17, 3) and found.now == (17, 0)
    assert found.saved()["second_listen"] is True


def test_not_twice_when_it_would_miss_one_of_yours() -> None:
    lost = [("Hey Sonu", "Hey Sonny."), *OWNER_SAID[1:]]  # without the hint, one of yours isn't the name
    found = train(lost, OWNER_LOOKALIKES)
    assert not found.second_listen and found.now == (17, 3)


def test_the_name_written_joined_without_the_hint() -> None:
    assert after_wake_word("Heisanu OpenKitty") == "OpenKitty"  # measured: Kokoro's voices, no hint
    assert after_wake_word("History is fun") is None


@pytest.mark.anyio
async def test_the_listener_listens_twice(tmp_path: Path) -> None:
    from tests.unit.test_wake import Script, listen, silence, speech

    use(set(), set(), set(), second_listen=True)
    script = Script("Hey Sonu, how are you?", "Hey Sunny, how are you?")
    assert await listen(tmp_path, silence(0.6) + speech(1.2) + silence(1.5), script) == []
    assert [hint for _, hint in script.asked] == [WAKE_HINT, ""]  # the second look: no hint
    script = Script("Hey Sonu, open kitty", "Hey, Sanu. Open kitty.")
    assert await listen(tmp_path, silence(0.6) + speech(1.2) + silence(1.5), script) == ["open kitty"]
