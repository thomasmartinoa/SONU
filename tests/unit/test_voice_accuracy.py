"""Scoring for `sonu voice check` (how well speech recognition hears the user)."""

from __future__ import annotations

import pytest

from sonu.voice.accuracy import SENTENCES, Score, word_errors, words


@pytest.mark.parametrize(
    ("expected", "heard", "errors"),
    [
        ("Open kitty on workspace two.", "open Kitty on Workspace 2", 0),  # case, punctuation, digits
        ("What's my battery right now?", "What\u2019s my battery right now", 0),  # a curly apostrophe
        ("Listen for Hey Sonu.", "Listen for Hey Soin", 0),  # SONU fixes the name itself (fix_name)
        ("Open kitty on workspace two.", "OpenKitty on Workspace 2.", 0),  # found on the laptop
        ("Listen for Hey Sonu.", "Listen for HeySanu.", 0),  # found on the laptop (large-v3)
        # Found on the laptop, with the owner's voice:
        ("Listen for Hey Sonu.", "Listen for his own.", 2),
        ("Yes.", "Peace", 1),
        ("Play Alone by Marshmello on YouTube.", "Play Alound by Marshmallow on YouTube", 2),
        ("Take a screenshot of this window.", "Take a screenshot", 3),  # words missed
        ("No.", "No, no.", 1),  # a word added
    ],
)
def test_word_errors(expected: str, heard: str, errors: int) -> None:
    assert word_errors(expected, heard) == errors


def test_score() -> None:
    score = Score("large-v3")
    score.add("Open kitty on workspace two.", "open kitty on workspace 2", 0.2)
    score.add("Yes.", "Peace", 0.4)
    assert (score.errors, score.total) == (1, 6)
    assert score.accuracy == pytest.approx(5 / 6)
    assert score.mean_seconds == pytest.approx(0.3)


def test_the_sentences_are_what_sonu_hears_every_day() -> None:
    assert any("Hey Sonu" in s for s in SENTENCES)
    assert {"yes", "no"} <= {w for s in SENTENCES for w in words(s)}  # short answers too
