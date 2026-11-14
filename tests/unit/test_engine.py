from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.audit import AuditLog
from sonu.config.settings import AppsSettings, AutonomyMode, Settings, VerifySettings, WebSettings
from sonu.core.engine import ConfirmRequest, Engine, enforce_verification
from sonu.core.planning import Plan
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.base import Choices, StepStatus, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.desktop_tools import DescribeDesktop, LaunchApp
from sonu.tools.registry import ToolRegistry
from sonu.verify import Check

REQUEST = "Open VSCodium and Chromium, put VSCodium on workspace 3 and Chromium on workspace 2"


@dataclass
class Confirmations:
    answer: bool
    seen: list[ConfirmRequest] = field(default_factory=list)

    async def __call__(self, request: ConfirmRequest) -> bool:
        self.seen.append(request)
        return self.answer


@dataclass
class Answers:
    """Answers SONU's which-one questions by label text (None = the user cancelled)."""

    pick: str | None
    seen: list[Choices] = field(default_factory=list)
    before_answering: Any = None

    async def __call__(self, choices: Choices) -> str | None:
        self.seen.append(choices)
        if self.before_answering:
            self.before_answering()
        if self.pick is None:
            return None
        return next(option.value for option in choices.options if self.pick in option.label)


@dataclass
class Harness:
    fake: FakeHyprland
    ctx: ToolContext
    audit: AuditLog

    def engine(self, plans: dict[str, Plan], autonomy: AutonomyMode = "confirm-risky") -> Engine:
        return Engine(
            planner=ScriptedPlanner(plans),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy(autonomy),
            audit=self.audit,
        )

    def workspace_of(self, window_class: str) -> list[int]:
        return sorted(w["workspace"]["id"] for w in self.fake.windows_of_class(window_class))


@pytest.fixture
async def harness(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Harness]:
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "nothing"),
        "XDG_CURRENT_DESKTOP": "Hyprland",
    }
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps.update(
        {
            "codium": FakeApp("VSCodium", "Welcome - VSCodium"),
            "chromium": FakeApp("chromium", "New Tab - Chromium"),
            "code-oss": FakeApp("code-oss", single_instance=True),
            "kitty": FakeApp("kitty", "zsh"),
            "zen-browser": FakeApp("zen"),
        }
    )
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    settings = Settings(
        apps=AppsSettings(launch_timeout_s=1.5),
        verify=VerifySettings(timeout_s=1.0),
        web=WebSettings(load_timeout_s=2.0),
    )
    resolver = AppResolver(catalog, role_defaults={"browser": "zen-browser", "terminal": "kitty"})
    ctx = ToolContext(desktop, resolver, settings, default_terminal="kitty", which=lambda n: f"/usr/bin/{n}")
    yield Harness(fake_hypr, ctx, AuditLog(tmp_path / "audit" / "audit.jsonl"))


def two_apps_plan() -> dict[str, Plan]:
    return {
        REQUEST: steps_plan(
            ("apps.launch", {"app": "VSCodium", "workspace": 3}),
            ("apps.launch", {"app": "Chromium", "workspace": 2}),
        )
    }


# ---------------------------------------------------------------- the flagship scenario
@pytest.mark.anyio
async def test_open_two_apps_on_workspaces(harness: Harness) -> None:
    report = await harness.engine(two_apps_plan()).run(REQUEST, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.workspace_of("VSCodium") == [3]
    assert harness.workspace_of("chromium") == [2]
    assert all(record.outcome.checks for record in report.steps)
    assert report.lines() == ["✔ Opened VSCodium on workspace 3", "✔ Opened Chromium on workspace 2"]
    kinds = [record.kind for record in harness.audit.records()]
    assert kinds == ["task.start", "task.plan", "step.result", "step.result", "task.end"]
    assert harness.audit.verify().ok


@pytest.mark.anyio
async def test_misplaced_window_is_moved_and_reported_honestly(harness: Harness) -> None:
    harness.fake.apps["chromium"] = FakeApp("chromium", honours_rules=False)  # lands on the focused ws
    report = await harness.engine(two_apps_plan()).run(REQUEST, confirm=Confirmations(False))
    assert report.status == "done"
    assert harness.workspace_of("chromium") == [2]
    assert "it opened on 1, so SONU moved the new window there" in report.steps[1].outcome.summary


@pytest.mark.anyio
async def test_already_open_single_instance_app_is_moved_not_relaunched(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "code-oss", "main.py - sonu", workspace_id=1, stable_id="a1")]
    plans = {"code on 3": steps_plan(("apps.launch", {"app": "code", "workspace": 3}))}
    report = await harness.engine(plans).run("code on 3", confirm=Confirmations(False))
    assert report.status == "done"
    assert harness.fake.launched == []
    assert harness.workspace_of("code-oss") == [3]
    assert report.steps[0].outcome.summary == "Moved the open Code - OSS window to workspace 3"


@pytest.mark.anyio
async def test_new_window_for_single_instance_app_is_moved_after_hand_off(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "code-oss", workspace_id=1, stable_id="a1")]
    plans = {"r": steps_plan(("apps.launch", {"app": "code", "workspace": 3, "new_window": True}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.workspace_of("code-oss") == [1, 3]  # hand-off ignored the rule; SONU moved it


# ---------------------------------------------------------------- honest failures
@pytest.mark.anyio
async def test_uninstalled_app_fails_but_other_steps_run(harness: Harness) -> None:
    plans = {
        "r": steps_plan(
            ("apps.launch", {"app": "Photoshop", "workspace": 2}),
            ("apps.launch", {"app": "kitty", "workspace": 4}),
        )
    }
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "partial"
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert "not installed" in report.steps[0].outcome.summary
    assert harness.workspace_of("kitty") == [4]


@pytest.mark.anyio
async def test_app_that_never_opens_a_window(harness: Harness) -> None:
    harness.fake.apps["kitty"] = FakeApp("kitty", opens_window=False)
    plans = {"r": steps_plan(("apps.launch", {"app": "kitty"}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "failed"
    assert "did not open a window" in report.steps[0].outcome.summary


@pytest.mark.anyio
async def test_unrecognised_window_is_unverified_not_done(harness: Harness) -> None:
    harness.fake.apps["kitty"] = FakeApp("Alacritty-ish")  # opens a window with an unexpected class
    plans = {"r": steps_plan(("apps.launch", {"app": "kitty"}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.steps[0].outcome.status is StepStatus.UNVERIFIED
    assert report.status == "unverified"  # never "done"; D39: not "failed" either — it did run
    assert report.headline() == "Done, but not confirmed — see the steps above"


@pytest.mark.anyio
async def test_ambiguous_app_asks_instead_of_guessing(harness: Harness) -> None:
    plans = {"r": steps_plan(("apps.launch", {"app": "VS Code", "workspace": 3}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "needs-input"
    assert report.questions == ["Which one did you mean for 'VS Code': Code - OSS or VSCodium?"]
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_invalid_arguments_and_unknown_tools_fail_safely(harness: Harness) -> None:
    plans = {
        "r": steps_plan(
            ("apps.launch", {"app": "kitty", "workspace": 0}),
            ("apps.launch", {"app": "kitty", "shell": "rm -rf ~"}),
            ("system.shell", {"command": "rm -rf ~"}),
        )
    }
    report = await harness.engine(plans).run("r", confirm=Confirmations(True))
    assert [record.outcome.status for record in report.steps] == [StepStatus.FAILED] * 3
    assert "workspace" in report.steps[0].outcome.summary
    assert "invalid arguments" in report.steps[1].outcome.summary
    assert "unknown tool" in report.steps[2].outcome.summary
    assert harness.fake.launched == []


# ---------------------------------------------------------------- policy & confirmation
@pytest.mark.anyio
async def test_close_needs_confirmation_and_respects_no(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "kitty", "zsh", stable_id="a1")]
    plans = {"close kitty": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    confirmations = Confirmations(False)
    report = await harness.engine(plans).run("close kitty", confirm=confirmations)
    assert [request.tool for request in confirmations.seen] == ["windows.close"]
    assert report.steps[0].outcome.status is StepStatus.DENIED
    assert report.status == "declined"
    assert report.headline() == "Nothing changed — not approved"
    assert len(harness.fake.windows) == 1


@pytest.mark.anyio
async def test_close_after_yes_is_verified(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "kitty", "zsh", stable_id="a1")]
    plans = {"close kitty": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    report = await harness.engine(plans).run("close kitty", confirm=Confirmations(True))
    assert report.status == "done"
    assert harness.fake.windows == []


@pytest.mark.anyio
async def test_window_that_refuses_to_close_is_reported(harness: Harness) -> None:
    harness.fake.apps["kitty"] = FakeApp("kitty", closes=False)
    harness.fake.windows = [make_window("0xa1", "kitty", stable_id="a1")]
    plans = {"r": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(True))
    assert report.status == "failed"
    assert "never force-kills" in report.steps[0].outcome.summary


@pytest.mark.anyio
async def test_trusted_mode_still_confirms_close(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "kitty", stable_id="a1")]
    plans = {"r": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    confirmations = Confirmations(True)
    await harness.engine(plans, autonomy="trusted").run("r", confirm=confirmations)
    assert len(confirmations.seen) == 1
    assert "window titles" in confirmations.seen[0].reason


@pytest.mark.anyio
async def test_suggest_mode_never_acts(harness: Harness) -> None:
    report = await harness.engine(two_apps_plan(), autonomy="suggest").run(
        REQUEST, confirm=Confirmations(True)
    )
    assert {record.outcome.status for record in report.steps} == {StepStatus.DENIED}
    assert report.status == "failed"  # forbidden by policy, not declined by the user
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_dry_run_changes_nothing(harness: Harness) -> None:
    report = await harness.engine(two_apps_plan()).run(REQUEST, confirm=Confirmations(True), dry_run=True)
    assert report.status == "planned"
    assert report.lines() == [
        "· would: Open VSCodium on workspace 3",
        "· would: Open Chromium on workspace 2",
    ]
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_ambiguous_close_asks_which(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "htop", workspace_id=2, stable_id="b2"),
    ]
    plans = {"r": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(True))
    assert report.status == "needs-input"
    assert "2 windows match kitty window" in report.questions[0]
    assert len(harness.fake.windows) == 2


# ---------------------------------------------------------------- other tools
@pytest.mark.anyio
async def test_move_focus_show_and_describe(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "zen", "GitHub", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "zsh", workspace_id=1, stable_id="b2"),
    ]
    plans = {
        "r": steps_plan(
            ("windows.move", {"window": {"app": "browser"}, "workspace": 2}),
            ("windows.focus", {"window": {"app": "kitty"}}),
            ("workspaces.show", {"workspace": 2}),
            ("desktop.describe", {}),
        )
    }
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.workspace_of("zen") == [2]
    assert harness.fake.active_workspace == 2
    describe = report.steps[3]
    assert "zen (title: 'GitHub')" in describe.outcome.summary
    # Window titles from the describe step never reach the audit log.
    audited = [r.data for r in harness.audit.records() if r.kind == "step.result" and r.data["index"] == 3]
    assert audited[0]["summary"] == "(personal content not logged)"


@pytest.mark.anyio
async def test_describe_only_this_workspace_when_the_user_says_so(harness: Harness) -> None:
    # found on real hardware: "what's open on this workspace?" listed every workspace
    harness.fake.windows = [
        make_window("0xa1", "zen", "GitHub", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "zsh", workspace_id=2, stable_id="b2"),
    ]
    plan = {"r": steps_plan(("desktop.describe", {}))}
    here = await harness.engine({"what's open on this workspace?": plan["r"]}).run(
        "what's open on this workspace?", confirm=Confirmations(False)
    )
    text = here.steps[0].outcome.summary
    assert "workspace 1: zen" in text and "kitty" not in text  # the active one only
    everywhere = await harness.engine(plan).run("r", confirm=Confirmations(False))
    assert "workspace 2: kitty" in everywhere.steps[0].outcome.summary  # "what's open?" still lists all
    harness.fake.windows = [make_window("0xb2", "kitty", "zsh", workspace_id=2, stable_id="b2")]
    empty = await harness.engine({"is this workspace empty?": plan["r"]}).run(
        "is this workspace empty?", confirm=Confirmations(False)
    )
    assert "no windows are open on workspace 1" in empty.steps[0].outcome.summary


@pytest.mark.anyio
async def test_moving_this_window_follows_it_but_only_when_the_user_says_so(harness: Harness) -> None:
    # found on real hardware: "move this window to workspace 2" moved it and left the user behind
    def windows() -> None:
        harness.fake.windows = [
            make_window("0xa1", "zen", "GitHub", workspace_id=1, stable_id="a1"),
            make_window("0xb2", "kitty", "zsh", workspace_id=1, stable_id="b2"),
        ]
        harness.fake.monitors[0]["activeWorkspace"] = {"id": 1, "name": "1"}
        harness.fake.active_address = "0xa1"  # the browser has the focus

    async def move(request: str, window: dict[str, Any]) -> Any:
        windows()
        return await harness.engine(
            {request: steps_plan(("windows.move", {"window": window, "workspace": 2}))}
        ).run(request, confirm=Confirmations(False))

    followed = await move("move this window to workspace 2", {"focused": True})
    assert followed.status == "done", followed.lines()
    assert harness.fake.active_workspace == 2  # the screen went with it
    assert "and went there" in followed.steps[0].outcome.summary
    quiet: list[tuple[str, dict[str, Any]]] = [
        ("move this window to workspace 2 and stay here", {"focused": True}),
        ("move this window to workspace 2 in the background", {"focused": True}),
        ("move zen to workspace 2", {"app": "zen"}),  # another window: the user stays where they are
    ]
    for request, window in quiet:
        report = await move(request, window)
        assert report.status == "done", report.lines()
        assert harness.fake.active_workspace == 1, request


@pytest.mark.anyio
async def test_move_by_title(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "zen", "GitHub - SONU", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "zen", "YouTube", workspace_id=1, stable_id="b2"),
    ]
    plans = {
        "r": steps_plan(
            ("windows.move", {"window": {"app": "zen", "title_contains": "github"}, "workspace": 5})
        )
    }
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert sorted((w["title"], w["workspace"]["id"]) for w in harness.fake.windows) == [
        ("GitHub - SONU", 5),
        ("YouTube", 1),
    ]


@pytest.mark.anyio
async def test_terminal_apps_run_in_the_default_terminal(harness: Harness) -> None:
    harness.fake.apps["kitty"] = FakeApp("btop")  # kitty running btop shows up with its own class here
    plans = {"r": steps_plan(("apps.launch", {"app": "btop"}))}
    await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert harness.fake.launched == [["kitty", "--", "btop"]]


# ---------------------------------------------------------------- non-step plans & safety nets
@pytest.mark.anyio
async def test_reply_ask_cannot(harness: Harness) -> None:
    plans = {
        "hi": Plan("reply", "Hello! What should I do?"),
        "do the thing": Plan("ask", "Which thing?"),
        "order pizza": Plan("cannot", "I can't order food."),
    }
    engine = harness.engine(plans)
    for request, status in (("hi", "reply"), ("do the thing", "ask"), ("order pizza", "cannot")):
        report = await engine.run(request, confirm=Confirmations(False))
        assert report.status == status
        assert report.lines() == [plans[request].message]


@pytest.mark.anyio
async def test_damaged_audit_log_blocks_actions(harness: Harness) -> None:
    harness.audit.path.parent.mkdir(parents=True, exist_ok=True)
    harness.audit.path.write_text("garbage\n")
    report = await harness.engine(two_apps_plan()).run(REQUEST, confirm=Confirmations(True))
    assert report.status == "error"
    assert "audit log is damaged" in (report.error or "")
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_hyprland_refusal_is_a_failed_step(harness: Harness) -> None:
    harness.fake.dispatch_handler = lambda argument: "error: no"
    plans = {"r": steps_plan(("apps.launch", {"app": "kitty"}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert "Hyprland refused" in report.steps[0].outcome.summary


def test_done_without_evidence_is_downgraded() -> None:
    launch, describe = LaunchApp(), DescribeDesktop()
    claimed = ToolOutcome(StepStatus.DONE, "Opened kitty")
    assert enforce_verification(launch, claimed).status is StepStatus.UNVERIFIED
    failing = ToolOutcome(StepStatus.DONE, "Opened kitty", (Check("kitty is open", False),))
    assert enforce_verification(launch, failing).status is StepStatus.FAILED
    assert enforce_verification(describe, claimed).status is StepStatus.DONE  # read-only


# ---------------------------------------------------------------- safety nets for small-model slips
@pytest.mark.anyio
async def test_users_words_pick_the_window_the_model_left_vague(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "zen", "GitHub - SONU", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "zen", "YouTube", workspace_id=1, stable_id="b2"),
    ]
    request = "move the GitHub window to workspace 4"
    plans = {request: steps_plan(("windows.move", {"window": {"app": "zen"}, "workspace": 4}))}  # no title
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert sorted((w["title"], w["workspace"]["id"]) for w in harness.fake.windows) == [
        ("GitHub - SONU", 4),
        ("YouTube", 1),
    ]


@pytest.mark.anyio
async def test_all_in_the_request_means_all(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "btop", workspace_id=2, stable_id="b2"),
    ]
    request = "close all the kitty windows"
    plans = {request: steps_plan(("windows.close", {"window": {"app": "kitty"}}))}  # model forgot all=true
    confirmations = Confirmations(True)
    report = await harness.engine(plans).run(request, confirm=confirmations)
    assert report.status == "done", report.lines()
    assert harness.fake.windows == []
    assert "kitty (workspace 1), kitty (workspace 2)" in confirmations.seen[0].description


@pytest.mark.anyio
async def test_title_words_passed_as_app_still_find_the_window(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "zen", "GitHub - SONU", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "zen", "YouTube", workspace_id=1, stable_id="b2"),
    ]
    plans = {"r": steps_plan(("windows.move", {"window": {"app": "GitHub"}, "workspace": 2}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert [w["workspace"]["id"] for w in harness.fake.windows if w["title"] == "GitHub - SONU"] == [2]


@pytest.mark.anyio
async def test_ambiguous_words_still_ask(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "zen", "GitHub - SONU", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "zen", "GitHub - other", workspace_id=1, stable_id="b2"),
    ]
    request = "move the github window to 4"
    plans = {request: steps_plan(("windows.move", {"window": {"app": "zen"}, "workspace": 4}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "needs-input"


@pytest.mark.anyio
async def test_planner_gets_app_labels(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "zen", "GitHub", stable_id="a1")]
    captured: dict[str, Any] = {}

    class Capture(ScriptedPlanner):
        async def plan(
            self,
            request: str,
            snapshot: Any,
            registry: Any,
            *,
            app_labels: Any = None,
            history: Any = (),
            notes: Any = (),
            tried: Any = (),
        ) -> Plan:
            captured["labels"] = app_labels
            return Plan("reply", "ok")

    engine = Engine(
        planner=Capture({}), registry=ToolRegistry(), ctx=harness.ctx, policy=Policy("confirm-risky")
    )
    await engine.run("hi", confirm=Confirmations(False))
    assert captured["labels"] == {"zen": "Zen Browser, your default browser"}


# ---------------------------------------------------------------- answering questions
@pytest.mark.anyio
async def test_user_picks_the_app_and_the_step_runs(harness: Harness) -> None:
    plans = {"r": steps_plan(("apps.launch", {"app": "VS Code", "workspace": 3}))}
    answers = Answers("VSCodium")
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=answers)
    assert report.status == "done", report.lines()
    assert answers.seen[0].prompt == "Which app did you mean by 'VS Code'?"
    assert [option.label for option in answers.seen[0].options] == ["Code - OSS", "VSCodium"]
    assert harness.workspace_of("VSCodium") == [3]
    assert report.steps[0].choice == "codium.desktop"
    step = next(r for r in harness.audit.records() if r.kind == "step.result")
    assert step.data["choice"] == "codium.desktop"


@pytest.mark.anyio
async def test_unanswered_question_changes_nothing(harness: Harness) -> None:
    plans = {"r": steps_plan(("apps.launch", {"app": "VS Code"}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=Answers(None))
    assert report.status == "needs-input"
    assert "no answer given" in report.steps[0].outcome.summary
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_failing_prompt_counts_as_no_answer(harness: Harness) -> None:
    async def broken(choices: Choices) -> str | None:
        raise EOFError

    plans = {"r": steps_plan(("apps.launch", {"app": "VS Code"}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=broken)
    assert report.status == "needs-input"
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_picked_window_is_closed_after_confirmation(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "htop", workspace_id=2, stable_id="b2"),
    ]
    plans = {"r": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    answers = Answers("htop")
    confirmations = Confirmations(True)
    report = await harness.engine(plans).run("r", confirm=confirmations, choose=answers)
    assert report.status == "done", report.lines()
    assert answers.seen[0].prompt == "Which window should I close?"
    assert answers.seen[0].options[-1].label == "All 2 of them"
    assert len(confirmations.seen) == 1  # answering a question is not approving a risky step
    assert [w["title"] for w in harness.fake.windows] == ["zsh"]


@pytest.mark.anyio
async def test_all_of_them_moves_every_match(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "htop", workspace_id=2, stable_id="b2"),
    ]
    plans = {"r": steps_plan(("windows.move", {"window": {"app": "kitty"}, "workspace": 4}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=Answers("All 2"))
    assert report.status == "done", report.lines()
    assert harness.workspace_of("kitty") == [4, 4]


@pytest.mark.anyio
async def test_answer_that_was_not_offered_is_rejected(harness: Harness) -> None:
    async def forged(choices: Choices) -> str | None:
        return "0xdead"

    harness.fake.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "htop", workspace_id=2, stable_id="b2"),
    ]
    plans = {"r": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(True), choose=forged)
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert len(harness.fake.windows) == 2


@pytest.mark.anyio
async def test_picked_window_that_closed_meanwhile_fails_safely(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1"),
        make_window("0xb2", "kitty", "htop", workspace_id=2, stable_id="b2"),
    ]

    def htop_exits() -> None:
        harness.fake.windows = [w for w in harness.fake.windows if w["title"] != "htop"]

    plans = {"r": steps_plan(("windows.move", {"window": {"app": "kitty"}, "workspace": 4}))}
    answers = Answers("htop", before_answering=htop_exits)
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=answers)
    assert report.steps[0].outcome.summary == "The window you picked is no longer open"
    assert harness.workspace_of("kitty") == [1]


@pytest.mark.anyio
async def test_dry_run_does_not_ask(harness: Harness) -> None:
    plans = {"r": steps_plan(("apps.launch", {"app": "VS Code"}))}
    answers = Answers("VSCodium")
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=answers, dry_run=True)
    assert report.status == "needs-input"
    assert answers.seen == []


# ---------------------------------------------------------------- found on the author's desktop
@pytest.mark.anyio
async def test_terminal_opens_a_new_window_even_if_one_is_open(harness: Harness) -> None:
    harness.fake.windows = [make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="a1")]
    harness.fake.active_address = "0xa1"  # the terminal the user is typing in
    plans = {"r": steps_plan(("apps.launch", {"app": "kitty", "workspace": 2}))}
    report = await harness.engine(plans).run("r", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.fake.launched
    assert harness.workspace_of("kitty") == [1, 2]  # the user's terminal stayed where it was


@pytest.mark.anyio
async def test_move_ignores_windows_already_on_the_target(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "kitty", "git pull", workspace_id=2, stable_id="a1"),
        make_window("0xb2", "kitty", "~", workspace_id=3, stable_id="b2"),
    ]
    plans = {"r": steps_plan(("windows.move", {"window": {"app": "kitty"}, "workspace": 3}))}
    answers = Answers(None)
    report = await harness.engine(plans).run("r", confirm=Confirmations(False), choose=answers)
    assert report.status == "done", report.lines()
    assert answers.seen == []  # nothing to ask: only one kitty wasn't on 3 already
    assert harness.workspace_of("kitty") == [3, 3]


@pytest.mark.anyio
async def test_choice_labels_are_short_and_mark_the_focused_window(harness: Harness) -> None:
    long_title = "cd ~/dev_works/SONU && git pull \\nsonu --dry-run open kitty on workspace 2"
    harness.fake.windows = [
        make_window("0xa1", "kitty", long_title, workspace_id=2, stable_id="a1"),
        make_window("0xb2", "kitty", "~", workspace_id=3, stable_id="b2"),
    ]
    harness.fake.active_address = "0xa1"
    plans = {"r": steps_plan(("windows.close", {"window": {"app": "kitty"}}))}
    answers = Answers(None)
    await harness.engine(plans).run("r", confirm=Confirmations(False), choose=answers)
    labels = [option.label for option in answers.seen[0].options]
    assert labels[0].startswith("kitty (workspace 2, focused) — cd ~/dev_works/SONU")
    assert labels[0].endswith("…") and len(labels[0]) < 90
    assert labels[1] == "kitty (workspace 3) — ~"


# ---------------------------------------------------------------- honest partial plans
@pytest.mark.anyio
async def test_parts_without_a_tool_are_reported_not_dropped(harness: Harness) -> None:
    request = "open kitty and turn on bluetooth"
    plans = {request: steps_plan(("apps.launch", {"app": "kitty"}), unsupported=("turn on bluetooth",))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "partial"  # every step worked, but the request isn't fully done
    assert report.lines()[-1] == "✖ Can't do this yet: turn on bluetooth"
    plan_record = next(r for r in harness.audit.records() if r.kind == "task.plan")
    assert plan_record.data["unsupported"] == ["turn on bluetooth"]


# ---------------------------------------------------------------- the terminal SONU runs in
@pytest.mark.anyio
async def test_the_terminal_sonu_runs_in_is_not_title_evidence(harness: Harness) -> None:
    request = "go to the youtube window"
    harness.fake.windows = [
        make_window("0xa1", "kitty", f"sonu {request}", workspace_id=2, pid=4242, stable_id="a1"),
        make_window("0xb2", "zen", "networkchuck - YouTube — Zen Browser", workspace_id=3, stable_id="b2"),
    ]
    harness.fake.active_address = "0xa1"
    harness.ctx.ancestors = lambda: frozenset({4242, 1})
    planner = ScriptedPlanner(
        {request: steps_plan(("windows.focus", {"window": {"title_contains": "youtube"}}))}
    )
    engine = Engine(planner=planner, registry=ToolRegistry(), ctx=harness.ctx, policy=Policy("confirm-risky"))
    report = await engine.run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.fake.active_address == "0xb2"
    seen = planner.seen[0][1]
    assert seen.origin_address == "0xa1"
    assert "kitty (the terminal SONU was started from)" in seen.describe()
    assert request not in seen.describe()


@pytest.mark.anyio
async def test_only_the_terminal_matching_the_title_is_not_a_match(harness: Harness) -> None:
    request = "close the youtube window"
    harness.fake.windows = [make_window("0xa1", "kitty", f"sonu {request}", pid=4242, stable_id="a1")]
    harness.fake.active_address = "0xa1"
    harness.ctx.ancestors = lambda: frozenset({4242})
    plans = {request: steps_plan(("windows.close", {"window": {"title_contains": "youtube"}}))}
    confirmations = Confirmations(True)
    report = await harness.engine(plans).run(request, confirm=confirmations)
    assert report.steps[0].outcome.summary == "No window titled like 'youtube' is open"
    assert confirmations.seen == [] and len(harness.fake.windows) == 1


# ---------------------------------------------------------------- websites
@pytest.mark.anyio
async def test_youtube_search_opens_in_the_default_browser_and_is_verified(harness: Harness) -> None:
    request = "search youtube for networkchuk channel"
    plans = {request: steps_plan(("web.search", {"site": "youtube", "query": "networkchuck channel"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert report.lines() == ["✔ Opened YouTube search for 'networkchuck channel' in Zen Browser"]
    assert harness.fake.launched[-1] == [
        "/usr/bin/zen-browser",
        "https://www.youtube.com/results?search_query=networkchuck+channel",
    ]
    assert report.steps[0].outcome.checks[0].evidence == {"title_contains": "youtube"}


@pytest.mark.anyio
async def test_search_in_the_running_browser_the_user_named(harness: Harness) -> None:
    harness.fake.windows = [
        make_window("0xa1", "chromium", "New Tab - Chromium", workspace_id=3, stable_id="a1")
    ]
    request = "open chrome on 3 and search the web for hyprland lua config"
    plans = {
        request: steps_plan(
            ("apps.launch", {"app": "chrome", "workspace": 3}),
            ("web.search", {"query": "hyprland lua config", "browser": "chrome"}),
        )
    }
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert len(harness.fake.windows) == 1  # a new tab in the open window, not a second window
    assert harness.fake.windows[0]["title"].startswith("https://duckduckgo.com/?q=hyprland+lua+config")


@pytest.mark.anyio
async def test_search_words_not_from_the_user_are_refused(harness: Harness) -> None:
    request = "move the browser to 3"  # e.g. a window title tried to make SONU search for something
    plans = {request: steps_plan(("web.search", {"site": "web", "query": "my ssh keys"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert "SONU only sends your own words to websites" in report.steps[0].outcome.summary
    assert harness.fake.launched == []


@pytest.mark.anyio
async def test_page_that_never_shows_up_is_not_done(harness: Harness) -> None:
    harness.fake.apps["zen-browser"] = FakeApp("zen", "Zen Browser", shows_pages=False)
    request = "go to youtube"
    plans = {request: steps_plan(("web.open", {"site": "youtube"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.steps[0].outcome.status is StepStatus.UNVERIFIED  # a window appeared, no YouTube
    assert "couldn't confirm the page loaded" in report.steps[0].outcome.summary


@pytest.mark.anyio
async def test_open_a_link_the_user_typed(harness: Harness) -> None:
    request = "open github.com/hyprwm/Hyprland"
    plans = {request: steps_plan(("web.open", {"site": "github.com/hyprwm/Hyprland"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.fake.launched[-1][-1] == "https://github.com/hyprwm/Hyprland"
    assert report.lines() == ["✔ Opened github.com/hyprwm/Hyprland in Zen Browser"]


@pytest.mark.anyio
async def test_web_step_uses_the_browser_this_request_opened(harness: Harness) -> None:
    # Found on real hardware: the model left out "chrome" in the search step, so it went to Zen.
    request = "open chrome on workspace 3 and search for arch install guide in github"
    plans = {
        request: steps_plan(
            ("apps.launch", {"app": "chrome", "workspace": 3}),
            ("web.search", {"site": "github", "query": "arch install guide"}),
        )
    }
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert report.lines()[-1] == "✔ Opened GitHub search for 'arch install guide' in Chromium"
    assert harness.fake.launched[-1][0] == "/usr/bin/chromium"


@pytest.mark.anyio
async def test_page_sonu_cannot_see_is_unverified_not_failed(harness: Harness) -> None:
    # The browser is running but the page never shows in any window title (e.g. a background tab).
    harness.fake.apps["zen-browser"] = FakeApp("zen", "Zen Browser", shows_pages=False)
    harness.fake.windows = [make_window("0xa1", "zen", "Zen Browser", workspace_id=1, stable_id="a1")]
    request = "search youtube for networkchuck"
    plans = {request: steps_plan(("web.search", {"site": "youtube", "query": "networkchuck"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    outcome = report.steps[0].outcome
    assert outcome.status is StepStatus.UNVERIFIED
    assert "it may be in a background tab" in outcome.summary


@pytest.mark.anyio
async def test_slow_page_is_still_verified(harness: Harness) -> None:
    # Found on real hardware: a page that took longer than the first wait was reported unconfirmed
    # although it loaded. Once the browser reacts, SONU waits for the page (web.load_timeout_s).
    # The tab opens at once, the page takes longer than an app launch may (1.5s here) to load.
    harness.fake.apps["zen-browser"] = FakeApp("zen", "Zen Browser", delay_s=0.1, page_load_s=2.5)
    harness.fake.windows = [make_window("0xa1", "zen", "Zen Browser", workspace_id=1, stable_id="a1")]
    harness.ctx.settings = harness.ctx.settings.model_copy(update={"web": WebSettings(load_timeout_s=5.0)})
    request = "open github.com/thomasmartinoa"
    plans = {request: steps_plan(("web.open", {"site": "github.com/thomasmartinoa"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()


@pytest.mark.anyio
async def test_site_operator_becomes_a_site_search(harness: Harness) -> None:
    # Found on real hardware: the model wrote "… site:github.com" instead of site="github".
    request = "open zen on workspace 3 and in github find me dotfiles of user thomasmartinoa"
    plans = {request: steps_plan(("web.search", {"query": "thomasmartinoa dotfiles site:github.com"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.fake.launched[-1][-1] == "https://github.com/search?q=thomasmartinoa+dotfiles"


@pytest.mark.anyio
async def test_a_github_username_is_a_user_search(harness: Harness) -> None:
    # Found on the laptop: "search for username X in github" searched repositories for "username X".
    request = "search for username thomasmartinoa in github"
    plans = {request: steps_plan(("web.search", {"query": "username thomasmartinoa", "site": "github"}))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.fake.launched[-1][-1] == "https://github.com/search?q=thomasmartinoa&type=users"
    assert "GitHub user search for 'thomasmartinoa'" in report.lines()[0]


@pytest.mark.anyio
async def test_search_goes_to_the_site_the_user_named(harness: Harness) -> None:
    # Found in an eval: the model said "Searching YouTube" but left site out (→ the web engine).
    request = "play faded on youtube"
    plans = {request: steps_plan(("web.search", {"query": "faded"}), unsupported=("start playing it",))}
    report = await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert harness.fake.launched[-1][-1] == "https://www.youtube.com/results?search_query=faded"
    assert report.status == "partial"


@pytest.mark.anyio
async def test_a_web_search_goes_to_the_site_the_user_named(harness: Harness) -> None:
    """Found in the evals (v38): "search for username thomasmartinoa in github" was planned as a web search
    for "username thomasmartinoa github"."""
    request = "search for username thomasmartinoa in github"
    plans = {request: steps_plan(("web.search", {"query": "username thomasmartinoa github"}))}
    await harness.engine(plans).run(request, confirm=Confirmations(False))
    assert harness.fake.launched[-1][-1] == "https://github.com/search?q=thomasmartinoa&type=users"


# ---------------------------------------------------------------- a second try (D92)
MOVE_ZEN = "move zen to workspace 4"


def recovering_engine(harness: Harness, recovery: Plan | None, first: Plan | None = None) -> Engine:
    planner = ScriptedPlanner(
        {MOVE_ZEN: first or steps_plan(("windows.move", {"window": {"app": "zen"}, "workspace": 4}))},
        recoveries={MOVE_ZEN: recovery} if recovery else None,
    )
    return Engine(
        planner=planner,
        registry=ToolRegistry(),
        ctx=harness.ctx,
        policy=Policy("confirm-risky"),
        audit=harness.audit,
    )


@pytest.mark.anyio
async def test_a_failed_step_gets_a_second_try(harness: Harness) -> None:
    """Zen isn't open, so it can't be moved: the second plan opens it on workspace 4 — the user's goal."""
    engine = recovering_engine(harness, steps_plan(("apps.launch", {"app": "zen", "workspace": 4})))
    report = await engine.run(MOVE_ZEN, confirm=Confirmations(True))
    assert report.status == "done", report.lines()
    assert harness.workspace_of("zen") == [4]
    lines = report.lines()
    assert lines[0].startswith("✖") and lines[1] == "↻ Tried another way:" and lines[2].startswith("✔")
    planner = engine.planner
    assert isinstance(planner, ScriptedPlanner) and planner.tried[0][0].startswith("✖")  # what it saw
    assert [r.kind for r in harness.audit.records()].count("task.retry") == 1


@pytest.mark.anyio
async def test_no_other_way_leaves_the_failure_said(harness: Harness) -> None:
    report = await recovering_engine(harness, None).run(MOVE_ZEN, confirm=Confirmations(True))
    assert report.status == "failed" and report.recovered_from is None
    assert "↻ Tried another way:" not in report.lines()


@pytest.mark.anyio
async def test_no_second_try_after_a_no_or_in_a_dry_run(harness: Harness) -> None:
    harness.fake.windows.append(make_window("0xa1", "kitty", "zsh", workspace_id=1, stable_id="k1"))
    first = steps_plan(
        ("windows.close", {"window": {"app": "kitty"}}),  # asked — and the user says no
        ("windows.move", {"window": {"app": "zen"}, "workspace": 4}),  # fails
    )
    retry = steps_plan(("apps.launch", {"app": "zen", "workspace": 4}))
    engine = recovering_engine(harness, retry, first)
    report = await engine.run(MOVE_ZEN, confirm=Confirmations(False))
    assert report.recovered_from is None and harness.workspace_of("zen") == []  # their no stands
    dry = await recovering_engine(harness, retry).run(MOVE_ZEN, confirm=Confirmations(True), dry_run=True)
    assert dry.recovered_from is None


@pytest.mark.anyio
async def test_a_second_try_never_redoes_what_worked(harness: Harness) -> None:
    first = steps_plan(
        ("apps.launch", {"app": "kitty", "workspace": 2}),
        ("windows.move", {"window": {"app": "zen"}, "workspace": 4}),
    )
    retry = steps_plan(
        ("apps.launch", {"app": "kitty", "workspace": 2}),  # already done: skipped
        ("apps.launch", {"app": "zen", "workspace": 4}),
    )
    report = await recovering_engine(harness, retry, first).run(MOVE_ZEN, confirm=Confirmations(True))
    assert report.status == "done"
    assert len(harness.fake.windows_of_class("kitty")) == 1  # one kitty, not two


@pytest.mark.anyio
async def test_a_browser_asked_for_on_a_workspace_opens_there_and_leaves_yours(harness: Harness) -> None:
    """Found on the laptop: "open my browser on workspace 2" moved the open Zen away from workspace 3."""
    harness.fake.windows = [make_window("0xb1", "zen", "Inbox — Zen Browser", workspace_id=3, stable_id="b1")]
    plans = {"open my browser on 2": steps_plan(("apps.launch", {"app": "browser", "workspace": 2}))}
    report = await harness.engine(plans).run("open my browser on 2", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.workspace_of("zen") == [2, 3]  # a new one on 2; yours still on 3
    plans = {"open my browser": steps_plan(("apps.launch", {"app": "browser"}))}
    launched = len(harness.fake.launched)
    await harness.engine(plans).run("open my browser", confirm=Confirmations(False))
    assert len(harness.fake.launched) == launched  # no workspace named: the open one is brought up


@pytest.mark.anyio
async def test_a_terminal_app_is_confirmed_by_the_terminal_it_runs_in(harness: Harness) -> None:
    """Found on the laptop: "open btop" opened it, then said it couldn't confirm — the window was kitty's."""
    plans = {"open btop": steps_plan(("apps.launch", {"app": "btop"}))}
    report = await harness.engine(plans).run("open btop", confirm=Confirmations(False))
    assert report.status == "done", report.lines()
    assert harness.fake.launched[-1][0].endswith("kitty")
