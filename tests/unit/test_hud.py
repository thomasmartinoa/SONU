"""SONU's HUD (D80): the state sonud writes for it, and answers clicked on it."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from sonu.core.engine import ConfirmRequest
from sonu.tools.base import Choice, Choices, Risk
from sonu.ui.hud import Hud, hud_chooser, hud_confirmer, hud_qml


def state(hud: Hud) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(hud.state_file.read_text())
    return data


def click(hud: Hud, value: Any, delay_s: float = 0.15) -> asyncio.Task[None]:
    """What the HUD's QML does on a button: write {id, value} to the answer file."""

    async def later() -> None:
        await asyncio.sleep(delay_s)
        question = state(hud)["question"]
        hud.answer_file.write_text(json.dumps({"id": question["id"], "value": value}) + "\n")

    return asyncio.ensure_future(later())


def test_the_qml_ships_with_sonu() -> None:
    text = hud_qml().read_text()
    assert 'WlrLayershell.namespace: "sonu-hud"' in text and "anchors.top: true" in text


def test_progress_becomes_what_the_hud_shows(tmp_path: Path) -> None:
    hud = Hud(tmp_path / "hud")
    hud.feed({"type": "listening"})
    assert state(hud)["state"] == "listening"
    assert stat.S_IMODE((tmp_path / "hud").stat().st_mode) == 0o700
    assert stat.S_IMODE(hud.state_file.stat().st_mode) == 0o600
    hud.feed({"type": "heard", "text": "open kitty on 3 and set the volume to 30"})
    hud.feed({"type": "plan", "kind": "steps", "steps": 2})
    hud.feed({"type": "step_start", "description": "Open kitty on workspace 3"})
    assert state(hud)["steps"] == [{"text": "Open kitty on workspace 3", "status": "running"}]
    hud.feed({"type": "step", "line": "✔ Kitty is up on workspace 3", "status": "done"})
    hud.feed({"type": "step_start", "description": "Set the volume to 30%"})
    hud.feed({"type": "step", "line": "! Set the volume, but couldn't read it back", "status": "unverified"})
    shown = state(hud)
    assert shown["state"] == "acting" and shown["request"] == "open kitty on 3 and set the volume to 30"
    assert shown["steps"] == [
        {"text": "Kitty is up on workspace 3", "status": "done"},
        {"text": "Set the volume, but couldn't read it back", "status": "unverified"},
    ]
    seq = shown["seq"]
    hud.feed(
        {
            "type": "final",
            "status": "done",
            "before_headline": [],
            "headline": "Done",
            "report": {"reply": "Kitty's up."},
        }
    )
    assert (state(hud)["state"], state(hud)["result"]) == ("done", "Kitty's up.")
    assert state(hud)["seq"] > seq  # every change is a new sequence number: the HUD redraws
    hud.feed({"type": "final", "status": "reply", "before_headline": [], "headline": "", "report": {}})
    hud.begin("")
    hud.feed({"type": "final", "status": "reply", "before_headline": [], "headline": "", "report": {}})
    assert state(hud)["state"] == "idle"  # a quiet end: nothing to show


@pytest.mark.anyio
async def test_yes_and_no_on_the_hud(tmp_path: Path) -> None:
    hud = Hud(tmp_path / "hud")
    confirm = hud_confirmer(hud)
    request = ConfirmRequest(
        tool="files.trash", description="Move 'a.pdf' to the trash", risk=Risk.MEDIUM, reason="r"
    )
    clicked = click(hud, True)
    assert await confirm(request) is True
    await clicked
    assert not hud.answer_file.exists() and state(hud)["question"] is None
    clicked = click(hud, False)
    assert await confirm(request) is False
    await clicked


@pytest.mark.anyio
async def test_only_an_answer_sonu_offered_counts(tmp_path: Path) -> None:
    hud = Hud(tmp_path / "hud")
    choose = hud_chooser(hud)
    choices = Choices(
        "Which invoice?", (Choice("invoice march.pdf", "/a"), Choice("invoice april.pdf", "/b"))
    )

    async def forged_then_real() -> None:
        await asyncio.sleep(0.15)
        question = state(hud)["question"]
        assert [o["label"] for o in question["options"]] == [
            "invoice march.pdf",
            "invoice april.pdf",
            "None of these",
        ]
        hud.answer_file.write_text(json.dumps({"id": question["id"], "value": "/etc/passwd"}))
        await asyncio.sleep(0.3)
        hud.answer_file.write_text(
            json.dumps({"id": question["id"] + 1, "value": "/a"})
        )  # another question's
        await asyncio.sleep(0.3)
        hud.answer_file.write_text(json.dumps({"id": question["id"], "value": "/b"}))

    task = asyncio.ensure_future(forged_then_real())
    assert await choose(choices) == "/b"
    await task


@pytest.mark.anyio
async def test_nobody_answers(tmp_path: Path) -> None:
    hud = Hud(tmp_path / "hud")
    assert await hud.ask("Go ahead?", [("Yes", True), ("No", False)], timeout_s=0.3) is None
    assert state(hud)["question"] is None
