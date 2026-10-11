"""What SONU says out loud (D79): paths, links, times and long lists said the way a person would."""

from __future__ import annotations

import pytest

from sonu.voice.speech import for_speech, shorten_list


@pytest.mark.parametrize(
    ("text", "said"),
    [
        # Measured with espeak-ng -x on the laptop: each of these was read symbol by symbol.
        ("Moved 'a.pdf' to ~/Documents", "Moved 'a.pdf' to Documents"),
        (
            "Copied the path of 'README.md' (~/dev_works/SONU)",
            "Copied the path of 'README.md' (SONU in dev works)",
        ),
        ("Opened ~ in Thunar", "Opened your home folder in Thunar"),
        (
            "Lentil soup — https://www.food.example/soup?id=4",
            "Lentil soup — food.example",
        ),  # a dash is a pause
        ("Invoice (yesterday 21:39)", "Invoice (yesterday 9 39 PM)"),
        ("at 09:05 and 18:00", "at 9 oh 5 AM and 6 PM"),
        ("song 2 of 12", "song 2 of 12"),  # nothing to change
        ("a ratio of 16:9", "a ratio of 16:9"),  # not a time
    ],
)
def test_for_speech(text: str, said: str) -> None:
    assert for_speech(text) == said


def test_long_lists_are_cut_short() -> None:
    line = "5 files match “invoice”: a.pdf; b.pdf; c.pdf; d.pdf; e.pdf"
    assert shorten_list(line) == "5 files match “invoice”: a.pdf; b.pdf; c.pdf; and 2 more"
    assert shorten_list("12 tabs: a; b; c; d; and 8 more") == "12 tabs: a; b; c; and 9 more"
    assert shorten_list("Media: playing — Song") == "Media: playing — Song"
    assert shorten_list("2 bookmarks: a; b") == "2 bookmarks: a; b"
