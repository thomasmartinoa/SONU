"""When the planner sees the conversation (D103): only when the request points back to it."""

from __future__ import annotations

import pytest

from sonu.core.conversation import refers_back


@pytest.mark.parametrize(
    "request_",
    [
        "move it to 4",
        "play that again",
        "the same on 3",
        "and on 2",
        "no, I meant 5",
        "4",
        "what about zen?",
        "what did you just say?",
        "another one",
        "close the other one",
        "put them side by side",
        "now 40",
        "on 5",
        "then zen",
        "no, 4",
    ],
)
def test_words_that_point_back(request_: str) -> None:
    assert refers_back(request_)


@pytest.mark.parametrize(
    "request_",
    # found on real hardware: each of these repeated an earlier request when the planner saw the conversation
    [
        "Play Something Relaxing",
        "Wow.",
        "hi sonu, long day huh",
        "can you book me a train ticket?",
        "search the arch wiki for pipewire",
        "open kitty on workspace 3",
        "open my parking-notes",
        "and the third art wiki for pipe wire.",  # "search the arch wiki", misheard
        "Okay, how are you?",
    ],
)
def test_requests_that_stand_alone(request_: str) -> None:
    assert not refers_back(request_)
