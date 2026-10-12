"""A correction mid-sentence (D89): applied once, before anything reads the user's words."""

from __future__ import annotations

from pathlib import Path

import pytest

from sonu.audit.log import AuditLog
from sonu.config.settings import Settings
from sonu.core.corrections import corrected
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.planning import Plan
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    ("said", "meant"),
    [
        ("open kitty on workspace 3, no, 4", "open kitty on workspace 4"),
        ("set the volume to 30, sorry, 40", "set the volume to 40"),
        ("open firefox, I mean zen", "open zen"),
        ("open firefox I mean zen", "open zen"),
        # found on the laptop: "spotify" in the words decided the service (D41) — it played on Spotify
        ("play lofi on spotify, actually on youtube", "play lofi on youtube"),
        ("play lofi on spotify, actually on youtube and turn it up", "play lofi on youtube and turn it up"),
        ("move this to workspace 2 wait no workspace 5", "move this to workspace 5"),
        ("set volume to 3, no, 4, sorry, 5", "set volume to 5"),
        ("remind me at 5, no, 6 to call mom", "remind me at 6 to call mom"),
        ("open the browser, I mean the terminal", "open the terminal"),
    ],
)
def test_a_correction_is_applied(said: str, meant: str) -> None:
    assert corrected(said) == meant


@pytest.mark.parametrize(
    "said",
    [
        "open kitty and no, I don't need anything else",
        "what's actually using my cpu",
        "sorry, can you open kitty",
        "open kitty, sorry to bother you",
        "no, open kitty",
        "is it raining, no idea",
        "open zen, actually",
        "turn off wifi, no wait",
        "open kitty",
        "remember that when I say vs code I mean code oss",  # defines a name (found by the golden evals)
        "by music I mean spotify",
    ],
)
def test_words_that_arent_a_correction_stay(said: str) -> None:
    assert corrected(said) == said


@pytest.mark.anyio
async def test_the_engine_plans_what_was_meant_and_audits_what_was_said(
    fake_hypr: FakeHyprland, tmp_path: Path
) -> None:
    planner = ScriptedPlanner({"open kitty on workspace 4": Plan("reply", "Okay.")})
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(),
        which=lambda name: None,
    )
    engine = Engine(
        planner=planner, registry=ToolRegistry(), ctx=ctx, policy=Policy("confirm-risky"), audit=audit
    )

    async def confirm(_: ConfirmRequest) -> bool:
        return True

    report = await engine.run("open kitty on workspace 3, no, 4", confirm=confirm)
    assert report.request == "open kitty on workspace 4" and ctx.request_text == "open kitty on workspace 4"
    [start] = [r for r in audit.records() if r.kind == "task.start"]
    assert start.data["said"] == "open kitty on workspace 3, no, 4"
