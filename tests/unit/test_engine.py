from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.audit import AuditLog
from sonu.config.settings import AppsSettings, AutonomyMode, Settings, VerifySettings
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
    settings = Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=1.0))
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
    assert "it appeared on 1; moved it" in report.steps[1].outcome.summary


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
    assert report.status == "failed"


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
        async def plan(self, request: str, snapshot: Any, registry: Any, *, app_labels: Any = None) -> Plan:
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
