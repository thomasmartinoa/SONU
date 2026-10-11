"""Reading the screen (M6 part 1): the local vision model only, visible windows only, the picture
deleted after, the answer shown to the user but kept away from SONU's models."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings, VerifySettings
from sonu.core.conversation import results_of
from sonu.core.engine import Engine
from sonu.core.report import TaskReport
from sonu.core.screen_reader import ScreenReader, is_local
from sonu.daemon.server import spoken_reply
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.llm.chat import ChatMessage, ChatResult
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


class Sees:
    """A vision model that answers one thing and records what it was shown."""

    name = "fake-vision"

    def __init__(self, answer: str = "The error says: permission denied for /etc/hosts.") -> None:
        self.answer, self.shown = answer, []  # type: ignore[var-annotated]

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        self.shown.append(messages)
        return ChatResult(self.answer, self.name, 0.1)

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        """Points (0–1000) at whatever it's asked for: the centre-left of the picture, or nowhere."""
        self.shown.append(messages)
        return ChatResult(json.dumps(self.points), self.name, 0.1)

    points: dict[str, Any] = {"found": True, "x": 250, "y": 500}  # noqa: RUF012


@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    model: Sees

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]]) -> TaskReport:
        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )

        async def refuse(request: object) -> bool:
            raise AssertionError("reading the screen never asks")

        return await engine.run(request, confirm=refuse)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    kitty = make_window("0xa1", "kitty", "zsh", workspace_id=1)
    kitty["at"], kitty["size"] = [10, 40], [800, 600]
    fake_hypr.windows += [kitty, make_window("0xb2", "zen", "Zen Browser", workspace_id=2)]
    fake_hypr.active_address = "0xa1"
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    system = FakeSystem()
    model = Sees()
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")),
        Settings(verify=VerifySettings(timeout_s=0.5)),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=tmp_path),
        reader=ScreenReader(model, "http://127.0.0.1:11434"),
    )
    yield Rig(fake_hypr, system, ctx, model)


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_read_this_window(rig: Rig) -> None:
    request = "what does the error in this window say?"
    report = await rig.run(request, ("screen.read", {"question": request, "window": {"focused": True}}))
    assert report.status == "done"
    assert lines(report) == ["The error says: permission denied for /etc/hosts."]
    [call] = rig.system.calls
    assert call[:3] == ("grim", "-g", "10,40 800x600")  # just that window
    [messages] = rig.model.shown
    assert messages[-1].images and messages[-1].content == f"This is the kitty window. {request}"
    assert not await asyncio.to_thread(Path(call[-1]).exists)  # the picture is gone once it's read
    # From the screen: private — never in what SONU's reply writer or planner gets.
    assert report.steps[0].outcome.private and "permission denied" not in " ".join(results_of(report))


@pytest.mark.anyio
async def test_the_whole_screen(rig: Rig) -> None:
    report = await rig.run("what's on my screen?", ("screen.read", {"question": "what's on my screen?"}))
    assert report.status == "done" and rig.system.calls[0][:2] == ("grim", str(rig.system.calls[0][1]))
    assert rig.model.shown[0][-1].content == "This is the user's screen. what's on my screen?"


@pytest.mark.anyio
async def test_a_window_on_another_workspace_is_shown_then_read(rig: Rig) -> None:
    # Found on real hardware: "what is open in zen?" (Zen on another workspace) was planned as only a
    # read, which failed, or only a focus, which never answered.
    report = await rig.run(
        "what does zen show?", ("screen.read", {"question": "what does zen show?", "window": {"app": "zen"}})
    )
    assert report.status == "done", lines(report)
    assert report.steps[0].description == "Show zen (workspace 2) and look at it"
    assert rig.fake.active_workspace == 2 and rig.model.shown[0][-1].content.startswith(
        "This is the zen window."
    )


@pytest.mark.anyio
async def test_only_a_model_on_this_laptop_sees_the_screen(rig: Rig) -> None:
    rig.ctx.reader = ScreenReader(rig.model, "http://192.168.1.20:11434")
    report = await rig.run("what's on my screen?", ("screen.read", {"question": "what's on my screen?"}))
    assert report.status == "failed" and "only read by a model on this laptop" in lines(report)[0]
    assert rig.model.shown == []


def test_is_local() -> None:
    assert (
        is_local("http://127.0.0.1:11434") and is_local("http://localhost:11434") and is_local("[::1]:11434")
    )
    assert not is_local("http://192.168.1.20:11434") and not is_local("https://ollama.example.com")


@pytest.mark.anyio
async def test_by_voice_the_screen_answer_is_read_out_as_is(rig: Rig) -> None:
    report = await rig.run("what's on my screen?", ("screen.read", {"question": "what's on my screen?"}))
    assert report.reply == ""  # no model rewrote it
    assert spoken_reply(report) == "The error says: permission denied for /etc/hosts."


# ---------------------------------------------------------------------------- pressing a button (part 2)
def spotify_controls() -> list[dict[str, object]]:
    def button(path: str, name: str, **extra: object) -> dict[str, object]:
        return {
            "path": path,
            "name": name,
            "role": "push button",
            "actions": ["click"],
            "showing": True,
            "enabled": True,
            **extra,
        }

    return [
        button("0/1/0", "Play", then="Pause"),
        button("0/1/1", "Next"),
        button("0/1/2", "Previous"),
        button("0/2/0", "Delete playlist"),
        button("0/3/0", "Next up", showing=False),
        {
            "path": "0/4",
            "name": "Shuffle",
            "role": "toggle button",
            "actions": ["click"],
            "showing": True,
            "enabled": False,
        },
    ]


@pytest.fixture
def spotify(rig: Rig) -> Rig:
    window = make_window("0xc3", "Spotify", "Spotify Premium", workspace_id=1, pid=4242)
    rig.fake.windows.append(window)
    rig.fake.active_address = "0xc3"
    rig.system.a11y_apps[4242] = spotify_controls()
    return rig


async def press(
    rig: Rig, request: str, control: str, *, approve: bool = True, **window: Any
) -> tuple[TaskReport, list[Any]]:
    asked: list[Any] = []

    async def confirm(request_: Any) -> bool:
        asked.append(request_)
        return approve

    engine = Engine(
        planner=ScriptedPlanner(
            {
                request: steps_plan(
                    ("screen.press", {"control": control, **({"window": window} if window else {})})
                )
            }
        ),
        registry=ToolRegistry(),
        ctx=rig.ctx,
        policy=Policy("confirm-risky"),
    )
    return await engine.run(request, confirm=confirm), asked


@pytest.mark.anyio
async def test_press_a_button_by_name(spotify: Rig) -> None:
    report, [asked] = await press(spotify, "press play in spotify", "play", app="spotify")
    assert report.status == "done", lines(report)
    assert asked.description == "Press “Play” (push button) in Spotify" and asked.risk.value == "medium"
    assert spotify.system.presses == ["Play:click"] and lines(report) == [
        "Pressed “Play” (push button) in Spotify"
    ]


@pytest.mark.anyio
async def test_nothing_is_pressed_without_approval(spotify: Rig) -> None:
    report, _ = await press(spotify, "press play", "play", approve=False)
    assert report.status == "declined" and spotify.system.presses == []


@pytest.mark.anyio
async def test_a_button_that_cant_be_taken_back_is_high_risk(spotify: Rig) -> None:
    _, [asked] = await press(spotify, "press delete playlist", "delete playlist")
    assert asked.risk.value == "high"


@pytest.mark.anyio
async def test_the_visible_one_wins_and_disabled_ones_are_never_pressed(spotify: Rig) -> None:
    await press(spotify, "press next", "next")
    assert spotify.system.presses == ["Next:click"]  # not the hidden "Next up"
    report, _ = await press(spotify, "press shuffle", "shuffle")
    assert report.status == "failed" and spotify.system.presses == ["Next:click"]


@pytest.mark.anyio
async def test_an_unknown_button_lists_what_there_is(spotify: Rig) -> None:
    report, _ = await press(spotify, "press frobnicate", "frobnicate")
    assert report.status == "failed"
    assert lines(report) == ["No “frobnicate” in Spotify — it has: Play, Next, Previous, Delete playlist"]
    assert report.steps[0].outcome.private  # the app's own words: shown, not given to SONU's models


@pytest.mark.anyio
async def test_nothing_changed_is_unverified(spotify: Rig) -> None:
    report, _ = await press(spotify, "press next", "next")
    assert report.status == "unverified"
    assert lines(report) == ["Pressed “Next” (push button) in Spotify, but couldn't see anything change"]


@pytest.mark.anyio
async def test_an_app_without_accessibility_says_so(rig: Rig) -> None:
    kitty = next(w for w in rig.fake.windows if w["class"] == "kitty")
    kitty["pid"] = 777
    rig.ctx.which = lambda name: None if name == "wlrctl" else f"/usr/bin/{name}"  # no pointer: no fallback
    report, _ = await press(rig, "press the menu button", "menu")
    assert report.status == "failed" and "doesn't show its buttons to SONU" in lines(report)[0]


@pytest.mark.anyio
async def test_the_kind_of_control_in_the_words_is_ignored(spotify: Rig) -> None:
    # Found on real hardware: "press the home button in thunar" → control "home button"; Thunar has "Home".
    await press(spotify, "press the next button", "next button")
    assert spotify.system.presses == ["Next:click"]


# ------------------------------------------------------------------- by looking: pointer + keys (part 3)
@pytest.mark.anyio
async def test_no_accessibility_clicks_where_it_looks_to_be(rig: Rig) -> None:
    kitty = next(w for w in rig.fake.windows if w["class"] == "kitty")
    kitty["pid"] = 777  # exposes nothing to accessibility
    report, [asked] = await press(rig, "click the settings icon in kitty", "settings icon", app="kitty")
    assert asked.risk.value == "high" and "it can miss" in asked.description
    assert asked.spoken == "click settings icon in kitty, found by looking — it may miss"
    assert report.status == "done", lines(report)
    # kitty is at (10, 40), 800×600: 250/1000 across, 500/1000 down → (210, 340).
    assert rig.fake.cursor == (210, 340) and rig.system.clicks == ["pointer click left"]
    assert lines(report) == ["Clicked where “settings icon” looked to be in kitty (the window changed)"]


@pytest.mark.anyio
async def test_a_click_that_changes_nothing_is_unverified(rig: Rig) -> None:
    next(w for w in rig.fake.windows if w["class"] == "kitty")["pid"] = 777
    rig.system.click_changes_screen = False
    report, _ = await press(rig, "click the settings icon in kitty", "settings icon", app="kitty")
    assert report.status == "unverified" and "it may have missed" in lines(report)[0]


@pytest.mark.anyio
async def test_nothing_found_nothing_clicked(rig: Rig) -> None:
    next(w for w in rig.fake.windows if w["class"] == "kitty")["pid"] = 777
    rig.model.points = {"found": False, "x": 0, "y": 0}
    report, _ = await press(rig, "click the settings icon in kitty", "settings icon", app="kitty")
    assert report.status == "failed" and rig.system.clicks == []
    assert lines(report) == ["Couldn't find “settings icon” in kitty's window"]


async def type_into(
    rig: Rig, request: str, text: str, *, approve: bool = True, **extra: Any
) -> tuple[TaskReport, list[Any]]:
    asked: list[Any] = []

    async def confirm(request_: Any) -> bool:
        asked.append(request_)
        return approve

    engine = Engine(
        planner=ScriptedPlanner({request: steps_plan(("screen.type", {"text": text, **extra}))}),
        registry=ToolRegistry(),
        ctx=rig.ctx,
        policy=Policy("confirm-risky"),
    )
    return await engine.run(request, confirm=confirm), asked


@pytest.mark.anyio
async def test_type_the_users_words_into_the_focused_window(rig: Rig) -> None:
    report, [asked] = await type_into(rig, "type hello world and press enter", "hello world", enter=True)
    assert (
        asked.risk.value == "high"
        and asked.description == "Type “hello world” into kitty as keystrokes and press Enter"
    )
    assert report.status == "done", lines(report)
    assert rig.system.typed == ["hello world", "-k Return"]  # the text through stdin, not the command line
    assert not any("hello" in " ".join(call) for call in rig.system.calls)


@pytest.mark.anyio
async def test_nothing_typed_without_approval_or_in_other_words(rig: Rig) -> None:
    report, _ = await type_into(rig, "type hello", "hello", approve=False)
    assert report.status == "declined" and rig.system.typed == []
    report, _ = await type_into(rig, "type hello", "rm -rf ~")
    assert report.status == "failed" and rig.system.typed == []


@pytest.mark.anyio
async def test_keys_go_to_the_terminal_this_request_opened(rig: Rig) -> None:
    # Found on real hardware (by voice): "open a terminal in workspace 3 and type hello" — the keys
    # belong in the new terminal, not in the window that was focused when the user spoke.
    request = "open kitty on workspace 3 and type hello"
    asked: list[Any] = []

    async def confirm(request_: Any) -> bool:
        asked.append(request_)
        return True

    plan = steps_plan(("apps.launch", {"app": "kitty", "workspace": 3}), ("screen.type", {"text": "hello"}))
    engine = Engine(
        planner=ScriptedPlanner({request: plan}),
        registry=ToolRegistry(),
        ctx=rig.ctx,
        policy=Policy("confirm-risky"),
    )
    rig.fake.apps["kitty"] = FakeApp("kitty", "zsh")
    focused_before = rig.fake.active_address
    report = await engine.run(request, confirm=confirm)
    assert report.status == "done", lines(report)
    new = next(w for w in rig.fake.windows if w["class"] == "kitty" and w["workspace"]["id"] == 3)
    assert new["address"] != focused_before and rig.fake.active_address == new["address"]
    assert rig.system.typed == ["hello"]


@pytest.mark.anyio
async def test_a_dry_run_names_the_window_an_earlier_step_would_open(rig: Rig) -> None:
    # Found on the laptop: the dry run of "open a terminal on workspace 3 and type hello" said
    # "Type “hello” into zen" — the window focused now; nothing is opened in a dry run.
    request = "open kitty on workspace 3 and type hello"
    plan = steps_plan(("apps.launch", {"app": "kitty", "workspace": 3}), ("screen.type", {"text": "hello"}))
    engine = Engine(
        planner=ScriptedPlanner({request: plan}),
        registry=ToolRegistry(),
        ctx=rig.ctx,
        policy=Policy("confirm-risky"),
    )

    async def never(_: Any) -> bool:
        raise AssertionError("a dry run asks nothing")

    report = await engine.run(request, confirm=never, dry_run=True)
    assert [record.description for record in report.steps] == [
        "Open kitty on workspace 3",
        "Type “hello” as keystrokes into the window from “Open kitty on workspace 3”",
    ]
    assert rig.system.typed == []


@pytest.mark.anyio
async def test_the_app_the_user_named_when_the_plan_named_no_window(rig: Rig) -> None:
    # Found in the evals (twice, after prompt changes): "what is open in zen?" was planned with no window,
    # and the whole screen was read. The user's words name the window.
    request = "what is open in zen?"
    report = await rig.run(request, ("screen.read", {"question": request}))
    assert report.status == "done", lines(report)
    assert report.steps[0].description == "Show zen (workspace 2) and look at it"
