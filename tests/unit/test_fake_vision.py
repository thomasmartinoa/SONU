"""The eval's stand-ins for SONU's models that look at things."""

from __future__ import annotations

import pytest

from sonu.testing.fake_vision import FakeScreenReader


@pytest.mark.anyio
async def test_looking_for_a_control_finds_nothing() -> None:
    """Found by `sonu eval` (llama3.1:8b, clicking by looking): the real reader's checks crashed on the
    stand-in."""
    reader = FakeScreenReader()
    assert await reader.locate(b"png", "the Play button") is None
    assert reader.questions == [("locate", "the Play button")]
