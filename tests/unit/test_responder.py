from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.core.conversation import PRIVATE, Conversation, Turn, results_of
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.planning import Plan
from sonu.core.report import StepRecord, TaskReport
from sonu.core.responder import Responder, acceptable, tidy
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.llm.chat import ChatMessage, ChatModelError, ChatResult
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import StepStatus, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@dataclass
class FakeTextModel:
    """Says a fixed line (or fails), and remembers what it was shown."""

    say: str = ""
    fail: bool = False
    name: str = "qwen3.5:4b"
    seen: list[list[ChatMessage]] = field(default_factory=list)

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        self.seen.append(messages)
        if self.fail:
            raise ChatModelError("Ollama is down")
        return ChatResult(content=self.say, model=self.name, duration_s=0.1, prompt_tokens=1, output_tokens=1)


def report_with(*outcomes: ToolOutcome, request: str = "r") -> TaskReport:
    report = TaskReport(request=request, plan=Plan("steps", "", ()))
    report.steps = [StepRecord(i, "t", {}, "d", outcome) for i, outcome in enumerate(outcomes)]
    return report


DONE = ToolOutcome(StepStatus.DONE, "Opened kitty on workspace 2")
FAILED = ToolOutcome(StepStatus.FAILED, "There's no folder called 'recipes' in your home")


# ---------------------------------------------------------------- the honesty guard
def test_a_failure_must_be_admitted_and_success_not_turned_into_one() -> None:
    failed, done = report_with(FAILED), report_with(DONE)
    assert not acceptable("All done, recipes is open!", failed, "")
    assert acceptable("I couldn't find a recipes folder in your home.", failed, "")
    assert acceptable("Kitty's up on workspace 2.", done, "workspace 2")
    assert not acceptable("I couldn't open kitty.", done, "workspace 2")


def test_numbers_must_come_from_the_results() -> None:
    done = report_with(ToolOutcome(StepStatus.DONE, "Battery: 76% (discharging)"))
    assert acceptable("You're at 76 percent and draining.", done, "Battery: 76% (discharging)")
    assert not acceptable("You're at 80 percent, about 3 hours left.", done, "Battery: 76% (discharging)")


def test_number_words_are_checked_too() -> None:
    done = report_with(ToolOutcome(StepStatus.DONE, "Moved kitty to workspace 4"))
    assert acceptable("Kitty's on workspace four now.", done, "move it to 4 Moved kitty to workspace 4")
    assert not acceptable("Kitty's on workspace five now.", done, "move it to 4 Moved kitty to workspace 4")


def test_filler_is_dropped() -> None:
    # Found on real hardware: the small model padded every reply.
    assert tidy("Kitty has been moved to workspace four and confirmed. Everything looks good.") == (
        "Kitty has been moved to workspace four."
    )
    assert tidy("Your battery is at 100 percent and not charging. All checked out.") == (
        "Your battery is at 100 percent and not charging."
    )
    assert tidy("All good!") == "All good!"  # never down to nothing


def test_not_a_sentence_is_rejected() -> None:
    done = report_with(DONE)
    for reply in ("", '{"kind": "steps"}', "# Done", "word " * 60, "Done.\nAlso this."):
        assert not acceptable(reply, done, "")


@pytest.mark.anyio
async def test_a_bad_or_missing_reply_falls_back_to_the_plain_result() -> None:
    report = report_with(FAILED)
    assert await Responder(FakeTextModel(say="Done! Opened recipes.")).reply(report) == (
        "There's no folder called 'recipes' in your home"
    )
    assert await Responder(FakeTextModel(fail=True)).reply(report_with(DONE)) == "Opened kitty on workspace 2"


@pytest.mark.anyio
async def test_private_results_never_reach_the_model() -> None:
    model = FakeTextModel(say="Kitty's up, and your clipboard is on screen.")
    report = report_with(DONE, ToolOutcome(StepStatus.DONE, "The clipboard has: hunter2", private=True))
    assert await Responder(model).reply(report) == "Kitty's up, and your clipboard is on screen."
    assert "hunter2" not in str(model.seen)
    assert PRIVATE in str(model.seen)


@pytest.mark.anyio
async def test_a_dry_run_needs_no_reply() -> None:
    model = FakeTextModel(say="Would open kitty.")
    report = report_with(ToolOutcome(StepStatus.SKIPPED, "Open kitty"))
    report.dry_run = True
    assert await Responder(model).reply(report) == "" and model.seen == []


@pytest.mark.anyio
async def test_only_private_results_need_no_reply() -> None:
    # Found on real hardware: "what's playing?" → "It's playing, but I can't see the song name."
    model = FakeTextModel(say="I can't see it.")
    report = report_with(ToolOutcome(StepStatus.DONE, "Media: playing — Faded — Alan Walker", private=True))
    assert await Responder(model).reply(report) == ""
    assert model.seen == []


@pytest.mark.anyio
async def test_small_talk_gets_a_conversational_answer_with_the_name() -> None:
    model = FakeTextModel(say="Pretty good, Martin! What are we up to?")
    report = TaskReport(request="hey sonu how's it going", plan=Plan("reply", "Hello."))
    reply = await Responder(model, name="Sonu", user_name="Martin").reply(report)
    assert reply == "Pretty good, Martin! What are we up to?"
    system = model.seen[0][0].content
    assert "You are Sonu" in system and "Martin" in system


# ---------------------------------------------------------------- the conversation
def test_the_conversation_fades_and_survives_a_restart(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "state" / "conversation.json"
    conversation = Conversation.load(path, idle_s=600, clock=lambda: now[0])
    conversation.add("open kitty on 2", "Kitty's on 2.", ("✔ Opened kitty on workspace 2",))
    assert oct(path.stat().st_mode)[-3:] == "600"
    again = Conversation.load(path, idle_s=600, clock=lambda: now[0])
    assert [t.request for t in again.recent()] == ["open kitty on 2"]
    now[0] += 601
    assert again.recent() == []


def test_private_results_are_not_remembered() -> None:
    report = report_with(ToolOutcome(StepStatus.DONE, "The clipboard has: hunter2", private=True), DONE)
    assert results_of(report) == (f"✔ {PRIVATE}", "✔ Opened kitty on workspace 2")


@pytest.fixture
async def engine_rig(
    fake_hypr: FakeHyprland, tmp_path: Path
) -> AsyncIterator[tuple[ToolContext, FakeSystem]]:
    system = FakeSystem()
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")),
        Settings(),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=tmp_path),
    )
    yield ctx, system


@pytest.mark.anyio
async def test_it_and_again_come_from_the_conversation(engine_rig: tuple[ToolContext, FakeSystem]) -> None:
    ctx, system = engine_rig
    system.other_players["spotify"] = ["Old Song", "Paused"]
    planner = ScriptedPlanner(
        {
            "play faded on spotify": steps_plan(("media.play", {"query": "faded", "service": "spotify"})),
            # The model took "faded" from the conversation — the user's own earlier words.
            "play it again on spotify": steps_plan(("media.play", {"query": "faded", "service": "spotify"})),
        }
    )
    model = FakeTextModel(say="Playing it again.")
    engine = Engine(
        planner=planner,
        registry=ToolRegistry(),
        ctx=ctx,
        policy=Policy("confirm-risky"),
        conversation=Conversation(),
        responder=Responder(model),
    )

    async def approve(request: ConfirmRequest) -> bool:
        return True

    first = await engine.run("play faded on spotify", confirm=approve)
    assert first.status == "done"
    second = await engine.run("play it again on spotify", confirm=approve)
    assert second.status == "done"  # "faded" was the user's word a moment ago: allowed
    history: list[Any] = planner.histories[1]
    assert [turn.request for turn in history] == ["play faded on spotify"]
    assert isinstance(history[0], Turn) and history[0].results == (
        "✔ Playing 'Faded' by Some Artist on Spotify",
    )
    assert second.reply == "Playing it again."


def test_a_reply_never_claims_an_action_the_results_dont_show() -> None:
    # Found on real hardware: "merge the pull request in SONU" was planned as only listing it, and
    # the reply said "I merged PR #31" — nothing was merged.
    listed = report_with(
        ToolOutcome(StepStatus.DONE, "1 open pull request in SONU: SONU #31 “M4 part 3” — checks passing"),
        request="merge the pull request in SONU",
    )
    allowed = "merge the pull request in SONU 1 open pull request SONU #31 checks passing"
    assert not acceptable("I merged PR #31, and all its checks are now passing.", listed, allowed)
    assert acceptable("PR #31 in SONU is open, and its checks are passing.", listed, allowed)
    assert acceptable("I haven't merged it — PR #31 is open with checks passing.", listed, allowed)
    merged = report_with(ToolOutcome(StepStatus.DONE, "Merged SONU #31 on GitHub"))
    assert acceptable("Merged — PR #31 is in.", merged, "Merged SONU #31 on GitHub")
    sent = report_with(ToolOutcome(StepStatus.DONE, "Opened kitty on workspace 2"))
    assert not acceptable("Kitty's open, and I sent your message.", sent, "workspace 2")


def test_after_a_failure_a_reply_promises_nothing() -> None:
    # Found on real hardware: "what is open in zen?" (Zen on another workspace) → "Zen isn't showing
    # up yet; I'll get it onto your screen." — nothing more happened.
    failed = report_with(
        ToolOutcome(StepStatus.FAILED, "zen is on workspace 3, not on screen — show it first")
    )
    allowed = "zen is on workspace 3, not on screen — show it first"
    assert not acceptable("Zen isn't showing up yet; I'll get it onto your screen.", failed, allowed)
    assert not acceptable("Zen isn't on screen — let me bring it over.", failed, allowed)
    assert acceptable(
        "Zen is on workspace 3, so I couldn't see it — show it first and ask again.", failed, allowed
    )
    done = report_with(ToolOutcome(StepStatus.DONE, "I'll remember that “editor” means Visual Studio Code"))
    assert acceptable(
        "Got it, I'll remember that editor means Visual Studio Code.", done, "editor Visual Studio Code"
    )
