"""Choosing from SONU's list by voice (phase 2): what the owner says, as the recognisers wrote it."""

from __future__ import annotations

import pytest

from sonu.tools.base import Choice, Choices
from sonu.voice.choosing import Picked, hint_for, pick, said_label, spoken_list

FILES = (
    Choice("Resume.pdf in Documents", "/docs/Resume.pdf"),
    Choice("Resume.pdf in Downloads", "/downloads/Resume.pdf"),
    Choice("resume-old.pdf in Desktop", "/desktop/resume-old.pdf"),
)
PACKAGES = (
    Choice("cozy-stack — extra: Digital home: brings all your web services", "extra/cozy-stack"),
    Choice("cowsay — extra: Configurable talking cow", "extra/cowsay"),
)


@pytest.mark.parametrize(
    ("heard", "value"),
    [
        # The bench's answer lines, as Whisper and Sarvam wrote them for the owner (2026-10-19)
        ("The second one.", "/downloads/Resume.pdf"),
        ("Number three.", "/desktop/resume-old.pdf"),
        ("No. 3", "/desktop/resume-old.pdf"),
        ("the last one.", "/desktop/resume-old.pdf"),
        ("The one in Downloads.", "/downloads/Resume.pdf"),
        ("the 1-min Downloads.", "/downloads/Resume.pdf"),  # "the one in Downloads"
        ("This is second one.", "/downloads/Resume.pdf"),
        ("first", "/docs/Resume.pdf"),
        ("Two.", "/downloads/Resume.pdf"),
        ("the one on the desktop", "/desktop/resume-old.pdf"),
    ],
)
def test_an_answer_picks_one_of_the_offered(heard: str, value: str) -> None:
    assert pick(heard, FILES) == Picked("pick", value)


def test_a_name_and_a_name_heard_another_way() -> None:
    assert pick("cowsay", PACKAGES) == Picked("pick", "extra/cowsay")
    assert pick("CowSay", PACKAGES) == Picked("pick", "extra/cowsay")  # how Whisper wrote it on the laptop
    assert pick("Cow say", PACKAGES) == Picked("pick", "extra/cowsay")
    assert pick("cozy stack", PACKAGES) == Picked("pick", "extra/cozy-stack")


@pytest.mark.parametrize("heard", ["none", "None of these.", "cancel that", "No.", "never mind"])
def test_none(heard: str) -> None:
    assert pick(heard, FILES) == Picked("none")


@pytest.mark.parametrize(
    "heard",
    [
        "",
        "Namaste John.",  # "the first one", as turbo wrote it: asked again, never guessed
        "the fifth one",  # there are three
        "not the first, the second",
        "Resume",  # every option has it
        "open kitty",
    ],
)
def test_anything_unclear_is_asked_again_never_guessed(heard: str) -> None:
    assert pick(heard, FILES) == Picked("unclear")


def test_what_sonu_says() -> None:
    said = spoken_list(Choices("Which package for 'cozy'?", PACKAGES))
    assert said == (
        "Which package for 'cozy'? There are 2: first, cozy-stack; second, cowsay. Say which one, or none."
    )
    many = spoken_list(Choices("Which file?", FILES * 3))
    assert many.startswith("Which file? There are 9: first, Resume.pdf in Documents; second, ")
    assert "third, resume-old.pdf in Desktop — the rest are on the screen" in many
    assert said_label("cowsay — extra: Configurable talking cow") == "cowsay"
    assert "cozy-stack" in hint_for(Choices("Which?", PACKAGES))
