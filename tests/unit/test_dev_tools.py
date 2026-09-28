"""The developer operator (M4): folders, Claude Code in SONU-owned terminals, typing, /model, reading.

The fake Claude Code copies what the real one (2.1.283) showed on the owner's laptop.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.claude_code import PROMPT, ClaudeScreen
from sonu.integrations.kitty import SonuTerminals
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    home: Path
    confirmations: list[ConfirmRequest]

    async def run(self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)

    def claude(self, folder: str) -> Any:
        found = self.system.kitty.claude(folder)
        assert found is not None, f"no Claude Code in {folder}"
        return found


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    for folder in ("dev_works/SONU", "dev_works/blog", "notes"):
        (home / folder).mkdir(parents=True)
    write_desktop_files(tmp_path / "share" / "applications")
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "share"), "XDG_DATA_DIRS": str(tmp_path / "x")}
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps["kitty"] = FakeApp("kitty", "zsh")
    system = FakeSystem()
    fake_hypr.on_exec = system.kitty.launched
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(catalog),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=0.5)),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(system, home=home),
        terminals=SonuTerminals(tmp_path / "run" / "terminals", system),
    )
    yield Rig(fake_hypr, system, ctx, home, [])


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


# ---------------------------------------------------------------------------- folder + Claude Code
@pytest.mark.anyio
async def test_make_a_folder_and_open_claude_code_there(rig: Rig) -> None:
    request = "make a folder called shop-app in dev_works and open claude code there on workspace 3"
    report = await rig.run(
        request,
        ("files.make_folder", {"name": "shop-app", "inside": "dev_works"}),
        ("dev.claude_open", {"workspace": 3}),
    )
    assert report.status == "done", lines(report)
    assert (rig.home / "dev_works" / "shop-app").is_dir()
    assert lines(report) == [
        "Made the folder ~/dev_works/shop-app",
        "Opened Claude Code (Haiku 4.5) in ~/dev_works/shop-app on workspace 3",
    ]
    # A folder this request made is new and empty: Claude Code's trust question is answered, unasked.
    assert rig.confirmations == []
    assert rig.claude("shop-app").state == "ready"
    window = rig.fake.windows_of_class("kitty")[0]
    assert window["title"] == "SONU · Claude Code · shop-app" and window["workspace"]["id"] == 3
    launched = rig.fake.launched[-1]
    assert "allow_remote_control=socket-only" in launched and "--listen-on" in launched  # this window only


@pytest.mark.anyio
async def test_an_untrusted_folder_is_trusted_only_after_asking(rig: Rig) -> None:
    report = await rig.run("open claude code in blog", ("dev.claude_open", {"folder": "blog"}), approve=False)
    assert report.status == "declined"
    [asked] = rig.confirmations
    assert asked.risk is Risk.MEDIUM and "trust the folder" in asked.description
    assert rig.fake.launched == []  # nothing started without the answer

    report = await rig.run("open claude code in blog", ("dev.claude_open", {"folder": "blog"}))
    assert report.status == "done" and rig.claude("blog").state == "ready"


@pytest.mark.anyio
async def test_a_folder_already_trusted_opens_without_asking(rig: Rig) -> None:
    folder = rig.home / "dev_works" / "SONU"
    (rig.home / ".claude.json").write_text(
        json.dumps({"projects": {str(folder): {"hasTrustDialogAccepted": True}}})
    )
    rig.system.kitty.trusted.add(str(folder))
    report = await rig.run("open claude code in sonu", ("dev.claude_open", {"folder": "sonu"}))
    assert report.status == "done" and rig.confirmations == []


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({"name": "../outside"}, "can't be a folder name"),
        ({"name": ".hidden"}, "can't be a folder name"),
        ({"name": "shop-app", "inside": "/etc"}, "couldn't find a folder called '/etc'"),
        ({"name": "invented"}, "must be your own words"),
    ],
)
@pytest.mark.anyio
async def test_folders_are_checked(rig: Rig, args: dict[str, Any], problem: str) -> None:
    request = "make a folder called shop-app in /etc and ../outside .hidden"
    report = await rig.run(request, ("files.make_folder", args))
    assert report.status == "failed" and problem in lines(report)[0]


@pytest.mark.anyio
async def test_a_folder_that_exists_is_said_so(rig: Rig) -> None:
    report = await rig.run("make a folder called notes", ("files.make_folder", {"name": "notes"}))
    assert report.status == "done" and lines(report) == ["~/notes already existed"]


# ---------------------------------------------------------------------------- typing into it
async def open_in(rig: Rig, folder: str) -> None:
    (rig.home / "dev_works" / folder).mkdir(exist_ok=True)
    request = f"make a folder called {folder} in dev_works and open claude code there"
    report = await rig.run(
        request, ("files.make_folder", {"name": folder, "inside": "dev_works"}), ("dev.claude_open", {})
    )
    assert report.status == "done", lines(report)


@pytest.mark.anyio
async def test_the_users_words_are_shown_asked_and_sent(rig: Rig) -> None:
    await open_in(rig, "shop-app")
    request = "tell it: build a landing page for my shop"
    report = await rig.run(request, ("dev.type", {"text": "build a landing page for my shop"}))
    assert report.status == "done", lines(report)
    [asked] = rig.confirmations
    assert asked.risk is Risk.MEDIUM
    assert (
        asked.description
        == "Type into Claude Code in shop-app and send it: “build a landing page for my shop”"
    )
    assert rig.claude("shop-app").sent == ["build a landing page for my shop"]
    assert lines(report) == ["Sent it to Claude Code in shop-app"]


@pytest.mark.anyio
async def test_words_that_arent_the_users_are_never_typed(rig: Rig) -> None:
    await open_in(rig, "shop-app")
    report = await rig.run("tell it to build a page", ("dev.type", {"text": "delete every file"}))
    assert report.status == "failed" and rig.claude("shop-app").sent == []


@pytest.mark.anyio
async def test_nothing_is_typed_without_a_sonu_terminal(rig: Rig) -> None:
    # The user's own kitty is open and focused: SONU never types into it.
    rig.fake.windows.append(make_window("0xc3", "kitty", "zsh", workspace_id=1))
    report = await rig.run("tell it hello", ("dev.type", {"text": "hello"}))
    assert report.status == "failed"
    assert "I haven't opened Claude Code anywhere yet" in lines(report)[0]
    assert not any(call[0] == "kitty" for call in rig.system.calls)


@pytest.mark.anyio
async def test_the_terminal_named_by_its_folder(rig: Rig) -> None:
    await open_in(rig, "shop-app")
    await open_in(rig, "blog-site")  # used last
    report = await rig.run(
        "tell the shop app one hello", ("dev.type", {"text": "hello", "terminal": "shop app"})
    )
    assert report.status == "done"
    assert rig.claude("shop-app").sent == ["hello"] and rig.claude("blog-site").sent == []
    report = await rig.run("tell it bye", ("dev.type", {"text": "bye"}))  # "it": the one used last
    assert rig.claude("shop-app").sent == ["hello", "bye"]


@pytest.mark.anyio
async def test_a_closed_claude_code_is_not_typed_into(rig: Rig) -> None:
    await open_in(rig, "shop-app")
    rig.claude("shop-app").state = "closed"
    report = await rig.run("tell it hello", ("dev.type", {"text": "hello"}))
    assert report.status == "failed"


# ---------------------------------------------------------------------------- /model and reading
@pytest.mark.anyio
async def test_switch_the_model_for_this_session_only(rig: Rig) -> None:
    await open_in(rig, "shop-app")
    report = await rig.run("switch it to sonnet", ("dev.claude_model", {"model": "sonnet"}))
    assert report.status == "done", lines(report)
    assert lines(report) == ["Claude Code in shop-app is on Sonnet 5 now (for this session)"]
    claude = rig.claude("shop-app")
    # Found on the laptop: "/model sonnet" + Enter also saved Sonnet as the user's default.
    assert (claude.model, claude.default) == ("Sonnet 5", "Haiku 4.5")
    report = await rig.run("switch it to haiku", ("dev.claude_model", {"model": "haiku"}))
    assert report.status == "done" and claude.model == "Haiku 4.5"


@pytest.mark.anyio
async def test_what_claude_is_doing(rig: Rig) -> None:
    await open_in(rig, "shop-app")
    report = await rig.run("is claude done?", ("dev.read", {}))
    assert report.status == "done" and lines(report) == [
        "Claude Code in shop-app (Haiku 4.5) is waiting for you"
    ]
    rig.claude("shop-app").working = True
    report = await rig.run("is claude done?", ("dev.read", {}))
    assert lines(report) == ["Claude Code in shop-app (Haiku 4.5) is working"]


def test_reading_the_real_screens() -> None:
    # Copied from Claude Code 2.1.283 on the owner's laptop.
    menu = ClaudeScreen(
        "   Select model\n"
        "     1.  Default (recommended)  Haiku 4.5\n"
        "     3.  Sonnet 5               Most efficient for everyday tasks\n"
        f"   {PROMPT} 5.  Haiku 4.5 ✔            Fastest for quick answers\n"
        "   ↓ 10. Opus 4.6               Best for everyday, complex tasks\n"
        "   Enter to set as default · s to use this session only · Esc to cancel\n"
    )
    assert menu.model_menu
    assert menu.menu_rows() == [
        ("Default (recommended)", False),
        ("Sonnet 5", False),
        ("Haiku 4.5", True),
        ("Opus 4.6", False),
    ]
    header = ClaudeScreen(
        " ▐▛███▛█   Claude Code v2.1.283\n▝▜██████▀  Sonnet 5 · Claude Pro\n"
        "  ⎿  Set model to Sonnet 5 for this session only\n"
    )
    assert header.model == "Sonnet 5" and header.model_set() == ["Sonnet 5 for this session only"]
    assert ClaudeScreen("typed: build a landing\n page for my shop").shows("build a landing page for my shop")


@pytest.mark.anyio
async def test_the_workspace_the_user_named_is_used_if_the_plan_leaves_it_out(rig: Rig) -> None:
    # Found on real hardware: "make a folder …, open claude code there on workspace 9 and tell it …"
    # was planned without the workspace; Claude Code opened on the focused workspace.
    request = (
        "make a folder called shop-app in dev_works, open claude code there on workspace 9 and tell it: hi"
    )
    report = await rig.run(
        request, ("files.make_folder", {"name": "shop-app", "inside": "dev_works"}), ("dev.claude_open", {})
    )
    assert report.status == "done", lines(report)
    assert lines(report)[1] == "Opened Claude Code (Haiku 4.5) in ~/dev_works/shop-app on workspace 9"
    assert rig.fake.windows_of_class("kitty")[0]["workspace"]["id"] == 9


# ---------------------------------------------------------------------------- the prompt writer
PROMPT_TEXT = (
    "Build a small todo app.\nRequirements:\n- add and remove tasks\n- a dark mode\n"
    "Approach:\n- look at what's in the folder first\nDone when: it runs and you show me how to start it."
)


class Writes:
    """A text model that writes a fixed prompt (or something unusable)."""

    name = "fake"

    def __init__(self, text: str = PROMPT_TEXT) -> None:
        self.text, self.asked = text, []  # type: ignore[var-annotated]

    async def complete_text(self, messages: list[Any], *, temperature: float, max_tokens: int) -> Any:
        from sonu.llm.chat import ChatResult

        self.asked.append(messages[-1].content)
        return ChatResult(self.text, "fake", 0.1)


@pytest.mark.anyio
async def test_a_prompt_from_the_idea_is_shown_in_full_asked_and_sent(rig: Rig) -> None:
    from sonu.core.prompt_writer import PromptWriter

    await open_in(rig, "todo-app")
    model = Writes()
    rig.ctx.writer = PromptWriter(model)
    request = "write the best prompt for my idea: a todo app with a dark mode"
    report = await rig.run(request, ("dev.prompt", {"idea": "a todo app with a dark mode"}))
    assert report.status == "done", lines(report)
    [asked] = rig.confirmations
    assert asked.risk is Risk.MEDIUM and asked.details == PROMPT_TEXT  # all of it, before asking
    assert asked.spoken == "send the prompt I wrote to Claude Code in todo-app — it's on your screen"
    assert "Project folder: todo-app\nMy idea: a todo app with a dark mode" in model.asked[0]
    assert rig.claude("todo-app").sent == [PROMPT_TEXT]  # a multi-line paste, sent whole
    assert lines(report) == ["Sent the prompt to Claude Code in todo-app"]


@pytest.mark.anyio
async def test_a_prompt_not_approved_is_not_sent(rig: Rig) -> None:
    from sonu.core.prompt_writer import PromptWriter

    await open_in(rig, "todo-app")
    rig.ctx.writer = PromptWriter(Writes())
    report = await rig.run(
        "write a prompt for a todo app", ("dev.prompt", {"idea": "a todo app"}), approve=False
    )
    assert report.status == "declined" and rig.claude("todo-app").sent == []


@pytest.mark.parametrize(("written", "problem"), [('{"prompt": 1}', "usable"), ("ok", "usable")])
@pytest.mark.anyio
async def test_an_unusable_prompt_is_never_offered(rig: Rig, written: str, problem: str) -> None:
    from sonu.core.prompt_writer import PromptWriter

    await open_in(rig, "todo-app")
    rig.ctx.writer = PromptWriter(Writes(written))
    report = await rig.run("write a prompt for a todo app", ("dev.prompt", {"idea": "a todo app"}))
    assert report.status == "failed" and problem in lines(report)[0] and rig.confirmations == []


def test_the_written_prompt_is_tidied() -> None:
    from sonu.core.prompt_writer import tidy

    assert tidy("```\n## Goal\n**Build** a todo app.\n```") == "Goal\nBuild a todo app."
