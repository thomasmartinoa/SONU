"""Training "Hey Sonu" on the owner's voice (D100): learn how Whisper writes their "Sonu", never a word that
would wake SONU on a look-alike phrase."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.voice.wake import after_wake_word, fix_name, learn_wake, load_learned, use

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

    heard = iter(
        ["Hey Sonu."] * 6
        + ["Hey Sonny, open kitty."] * 6
        + ["A Sonu, play music."] * 6
        + ["Hey son, come here."] * 12
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
    }
