"""`sonu voice bench` (phase 2, D105): the lines, what's kept, how a model is scored."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.voice import bench


def test_every_line_scores_full_marks_when_heard_exactly() -> None:
    """Each line's key words are in its own text — a model that hears it exactly gets them all."""
    ids = [line.id for line in bench.LINES]
    assert len(ids) == len(set(ids))
    assert {line.group for line in bench.LINES} == set(bench.GROUPS)
    for line in bench.LINES:
        if line.group == "free":
            assert line.prompt and not line.text and not line.keys
            continue
        assert line.keys, line.id
        assert all(bench.key_found(key, line.text) for key in line.keys), line.id
    assert 60 <= len(bench.LINES) <= 80


@pytest.mark.parametrize(
    ("key", "heard", "found"),
    [
        ("2", "Open kitty on workspace two.", True),
        ("30", "set the volume to 30%", True),
        ("35", "thirty five", True),
        ("vs code|vscode|visual studio code", "Open VSCode in the SONU folder", True),
        ("dev works", "open claude code in dev_works", True),
        ("dev works", "open claude code in devworks", True),
        ("sonu test", "sonu-test", True),
        ("kitty", "Open kittyon workspace 2", False),  # found on the laptop: "kitty on" → "kittyon"
        ("cowsay|cow say", "Install Coase", False),  # found on the laptop
        ("first|1st", "The second one.", False),  # "one" isn't "the first"
        ("sonu", "Hey, Sonu!", True),
        ("sonu", "Hey Sunny", False),  # the model is scored, not what SONU corrects after it
    ],
)
def test_key_words(key: str, heard: str, found: bool) -> None:
    assert bench.key_found(key, heard) is found


def test_word_errors() -> None:
    assert bench.word_errors("Set the volume to thirty percent.", "set the volume to 30%") == (0, 6)
    assert bench.word_errors("Install cowsay.", "Install Coase") == (1, 2)


def test_recordings_are_kept_privately_and_free_speech_needs_its_words(tmp_path: Path) -> None:
    folder = bench.bench_dir(tmp_path / "voice", "quiet")
    bench.save_recording(folder, "app-kitty", b"\x01\x00" * 1600)
    bench.remember_said(folder, "app-kitty", "Open kitty on workspace two.")
    bench.save_recording(folder, "free-song", b"\x01\x00" * 1600)  # recorded, words not typed yet
    assert [line.id for line in bench.recorded(folder)] == ["app-kitty"]
    assert bench.load_recording(folder, "app-kitty") == b"\x01\x00" * 1600
    for path in (tmp_path / "voice" / "bench", folder):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (folder / "app-kitty.wav", folder / "said.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_scores_by_group_with_the_misses() -> None:
    lines = [bench.BY_ID["pkg-cowsay"], bench.BY_ID["ans-yes"], bench.BY_ID["num-go"]]
    expected = {line.id: line.text for line in lines}
    heard = {
        "pkg-cowsay": ("Install Coase.", 0.4),
        "ans-yes": ("Yeah.", 0.2),
        "num-go": ("Go to workspace 3", 0.3),
    }
    result = bench.score("turbo", lines, expected, heard)
    assert result.all.keys == 4 and result.all.keys_right == 3
    assert result.groups["answers"].key_score == 1.0 and result.groups["names"].key_score == 0.5
    assert result.misses == [("pkg-cowsay", "Install Coase.", ["cowsay|cow say"])]
    assert round(result.all.mean_seconds, 2) == 0.3


def test_the_hint_is_sonus_own_never_the_lines_words() -> None:
    everyday = "Hey Sonu. Yes. No. Workspace, kitty, Zen Browser"
    assert bench.hint_for(bench.BY_ID["pkg-cowsay"], everyday) == everyday  # "cowsay" isn't given away
    with_choices = bench.hint_for(bench.BY_ID["ans-downloads"], everyday)
    assert with_choices.startswith(everyday) and "Resume.pdf in Downloads" in with_choices  # what's on screen


def test_cost() -> None:
    assert bench.cost_per_1000("sarvam", 4.0) == "₹33.33"
    assert bench.cost_per_1000("gemini-transcribe", 4.0) == "$0.33"
    assert bench.cost_per_1000("turbo", 4.0) == "free"
    assert bench.cost_per_1000("gemini", 4.0) == "?"


def test_report_from_what_was_heard(xdg_env: dict[str, str], tmp_path: Path) -> None:
    from sonu.cli.app import app

    folder = bench.bench_dir(Path(xdg_env["XDG_DATA_HOME"]) / "sonu" / "voice", "quiet")
    for line in (bench.BY_ID["app-kitty"], bench.BY_ID["ans-yes"]):
        bench.save_recording(folder, line.id, b"\x00\x00" * 16000)
        bench.remember_said(folder, line.id, line.text)
    bench.save_heard(
        folder, "turbo", {"app-kitty": ("Open KIDDI on workspace 2", 0.3), "ans-yes": ("Peace", 0.2)}
    )
    bench.save_heard(
        folder, "sarvam", {"app-kitty": ("Open kitty on workspace two", 0.6), "ans-yes": ("Yes", 0.5)}
    )
    (tmp_path / "run").mkdir(exist_ok=True)
    env = xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}

    result = CliRunner().invoke(app, ["voice", "bench", "report", "--json"], env=env)
    assert result.exit_code == 0, result.output
    scores = {s["model"]: s for s in json.loads(result.output)}
    assert scores["sarvam"]["key_words"] == 1.0 and scores["sarvam"]["answers"] == 1.0
    assert scores["turbo"]["key_words"] == pytest.approx(1 / 3) and scores["turbo"]["answers"] == 0.0

    result = CliRunner().invoke(app, ["voice", "bench", "report", "--misses"], env=env)
    assert result.exit_code == 0, result.output
    assert "“Yes.” → “Peace”" in result.output


def test_nothing_recorded_yet(xdg_env: dict[str, str], tmp_path: Path) -> None:
    from sonu.cli.app import app

    (tmp_path / "run").mkdir(exist_ok=True)
    env = xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}
    result = CliRunner().invoke(app, ["voice", "bench", "run", "-m", "turbo"], env=env)
    assert result.exit_code == 1 and "Nothing recorded yet" in result.output


def test_a_daily_limit_stops_that_model_and_keeps_what_it_heard(
    xdg_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found running the bench: Gemini's free tier allows 20 requests a day per model; the run waited on it
    four times a line, for every line, printing the whole error each time."""
    from sonu.cli import voice_bench_cmd
    from sonu.cli.app import app
    from sonu.voice.cloud_ears import CloudHearingError

    folder = bench.bench_dir(Path(xdg_env["XDG_DATA_HOME"]) / "sonu" / "voice", "quiet")
    for line in (bench.BY_ID["app-kitty"], bench.BY_ID["ans-yes"], bench.BY_ID["ans-no"]):
        bench.save_recording(folder, line.id, b"\x00\x00" * 16000)
        bench.remember_said(folder, line.id, line.text)

    class DailyLimit:
        calls = 0

        def transcribe(self, pcm16: bytes, hint: str = "") -> str:
            self.calls += 1
            if self.calls > 1:
                raise CloudHearingError(
                    "Google Gemini answered 429: You exceeded your current quota "
                    "[GenerateRequestsPerDayPerProjectPerModel-FreeTier: 20] retry in 76533s"
                )
            return "Open kitty on workspace 2."

    ears = DailyLimit()
    monkeypatch.setattr(voice_bench_cmd, "_make_ears", lambda name, ctx: (ears, True))
    monkeypatch.setattr(
        "sonu.cli.voice_bench_cmd.time.sleep", lambda s: pytest.fail("waited on a daily limit")
    )
    (tmp_path / "run").mkdir(exist_ok=True)
    env = xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}

    result = CliRunner().invoke(app, ["voice", "bench", "run", "-m", "gemini", "--yes"], env=env)
    assert result.exit_code == 0, result.output
    assert ears.calls == 2  # stopped at the first used-up answer
    assert len(bench.load_heard(folder, "gemini")) == 1  # what it heard is kept
    assert "used up" in result.output and "-m gemini" in result.output


def test_used_up_or_wait() -> None:
    assert bench.used_up("answered 429: quota [GenerateRequestsPerDayPerProjectPerModel-FreeTier: 20]")
    assert bench.used_up("answered 429: quota retry in 76533s")
    assert not bench.used_up("answered 429: quota retry in 20s")
    assert bench.wait_for_limit("answered 429: quota retry in 20s") == 21.0


def test_an_answer_is_scored_by_the_choice_sonu_would_make() -> None:
    # Found on real hardware: "Namaste John." for "The first one" has no key word and no choice;
    # "No. 3" has the key word wrong in spelling but is still the third option.
    lines = [bench.BY_ID[i] for i in ("ans-first", "ans-three", "ans-none", "ans-second")]
    expected = {line.id: line.text for line in lines}
    heard = {
        "ans-first": ("Namaste John.", 0.4),
        "ans-three": ("No. 3", 0.3),
        "ans-none": ("None of them.", 0.3),
        "ans-second": ("The first one.", 0.3),  # heard clearly, but the wrong one
    }
    result = bench.score("turbo", lines, expected, heard)
    assert (result.chosen, result.chosen_of) == (2, 4)
    assert result.wrong_choices == [("ans-first", "Namaste John."), ("ans-second", "The first one.")]
    assert bench.as_json([result])[0]["choices_understood"] == 0.5
