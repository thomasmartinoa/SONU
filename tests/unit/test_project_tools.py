"""Working on a project (D65): open it in VS Code, run its own commands, read git."""

from __future__ import annotations

import json
import subprocess
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
from sonu.integrations import run_task
from sonu.integrations.kitty import SonuTerminals
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.system.commands import SubprocessRunner
from sonu.system.git import changes, recent_commits
from sonu.system.projects import matching_tasks, project_tasks
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools import project_tools
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.project_tools import describe_changes, find_files
from sonu.tools.registry import ToolRegistry


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def installed(name: str) -> str | None:
    return f"/usr/bin/{name}"


# ---------------------------------------------------------------------------- a project's commands
def test_package_json_scripts_run_with_the_projects_package_manager(tmp_path: Path) -> None:
    write(tmp_path / "package.json", json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}))
    write(tmp_path / "pnpm-lock.yaml")
    tasks = project_tasks(tmp_path)
    assert [(t.argv, t.runs) for t in tasks] == [
        (("pnpm", "run", "test"), "vitest run"),
        (("pnpm", "run", "build"), "vite build"),
    ]


def test_makefile_targets_and_justfile_recipes(tmp_path: Path) -> None:
    write(
        tmp_path / "Makefile", ".PHONY: test\nCC := gcc\ntest: build\n\t./run\nbuild:\n\tcc x.c\n%.o: %.c\n"
    )
    write(tmp_path / "justfile", "set shell := ['bash']\nlint:\n  ruff check\n@fmt *args:\n  ruff format\n")
    assert [t.command for t in project_tasks(tmp_path)] == [
        "just lint",
        "just fmt",
        "make test",
        "make build",
    ]


def test_a_uv_project_gets_its_tests_lint_and_type_checks(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        "[project]\nname='x'\n[tool.ruff]\n[tool.mypy]\n[tool.pytest.ini_options]\n",
    )
    write(tmp_path / "uv.lock")
    tasks = project_tasks(tmp_path)
    assert [t.command for t in tasks] == ["uv run pytest", "uv run ruff check", "uv run mypy"]
    assert not any(t.listed for t in tasks)


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("tests", ["npm run test"]),
        ("the tests", ["npm run test"]),
        ("unit tests", ["npm run test"]),
        ("build", ["npm run build"]),
        ("type check", ["npm run typecheck"]),
        ("lint", ["npm run lint:fix", "npm run lint:check"]),  # several: the user picks
        ("deploy", []),
    ],
)
def test_the_users_words_pick_a_command_by_name(tmp_path: Path, words: str, expected: list[str]) -> None:
    scripts = {
        "test": "jest",
        "build": "tsc",
        "typecheck": "tsc --noEmit",
        "lint:fix": "x",
        "lint:check": "y",
    }
    write(tmp_path / "package.json", json.dumps({"scripts": scripts}))
    assert [t.command for t in matching_tasks(project_tasks(tmp_path), words, installed)] == expected


def test_a_listed_command_wins_over_an_inferred_one(tmp_path: Path) -> None:
    write(tmp_path / "Makefile", "test:\n\tpytest -x\n")
    write(tmp_path / "pyproject.toml", "[tool.pytest.ini_options]\n")
    write(tmp_path / "uv.lock")
    assert [t.command for t in matching_tasks(project_tasks(tmp_path), "tests", installed)] == ["make test"]


def test_the_helper_records_the_exit_code_not_the_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = tmp_path / "x.exit"
    code = run_task.main(
        [str(status), "python3", "-c", "print('[sonu] finished (exit 0)'); raise SystemExit(3)"]
    )
    assert code == 3
    assert status.read_text() == "3"
    assert "[sonu] finished (exit 3)" in capsys.readouterr().out
    assert run_task.main([str(status), "/nonexistent/program"]) == 127


# ---------------------------------------------------------------------------- files and git
def test_files_are_found_by_name_stem_or_the_end_of_their_path(tmp_path: Path) -> None:
    planner = write(tmp_path / "src/sonu/core/planner.py")
    write(tmp_path / "src/sonu/tools/registry.py")
    write(tmp_path / "node_modules/planner.py")
    write(tmp_path / ".venv/lib/planner.py")
    assert find_files(tmp_path, "planner.py") == [planner]
    assert find_files(tmp_path, "planner") == [planner]
    assert find_files(tmp_path, "core/planner.py") == [planner]
    assert find_files(tmp_path, "tools/planner.py") == []
    assert find_files(tmp_path, "../../etc/passwd") == []


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.mark.anyio
async def test_git_changes_and_commits_from_a_real_repository(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "T")
    write(tmp_path / "app.py", "a\nb\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "First commit")
    write(tmp_path / "app.py", "a\nc\nd\n")
    write(tmp_path / "new file.txt", "x")
    runner = SubprocessRunner()
    found = await changes(runner, tmp_path)
    assert describe_changes("proj", found) == (
        "proj (on main): 1 file changed, +2 -1: app.py +2 -1. 1 new untracked: new file.txt"
    )
    assert [c.subject for c in await recent_commits(runner, tmp_path)] == ["First commit"]


def test_a_clean_repository_says_so() -> None:
    from sonu.system.git import Changes

    assert describe_changes("SONU", Changes("main", "origin/main", 0, 2)) == (
        "SONU (on main, 2 behind origin/main): no uncommitted changes"
    )


# ---------------------------------------------------------------------------- the tools, end to end
@dataclass
class Rig:
    fake: FakeHyprland
    system: FakeSystem
    ctx: ToolContext
    home: Path
    confirmations: list[ConfirmRequest]

    async def run(
        self, request: str, *steps: tuple[str, dict[str, Any]], approve: bool = True, dry_run: bool = False
    ) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan(*steps)}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm, dry_run=dry_run)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    sonu = home / "dev_works" / "SONU"
    write(sonu / "pyproject.toml", "[tool.ruff]\n[tool.pytest.ini_options]\n")
    write(sonu / "uv.lock")
    write(sonu / "src/sonu/core/planner.py")
    write(sonu / "README.md")
    write(sonu / "docs/README.md")
    write(
        home / "dev_works/shop-app/package.json", json.dumps({"scripts": {"test": "vitest", "deploy": "x"}})
    )
    (home / "notes" / "SONU").mkdir(parents=True)  # a folder of the same name that isn't a project
    write_desktop_files(tmp_path / "share" / "applications")
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "share"), "XDG_DATA_DIRS": str(tmp_path / "x")}
    catalog = AppCatalog.load(env, which=installed)
    fake_hypr.apps["kitty"] = FakeApp("kitty", "zsh")
    fake_hypr.apps["code-oss"] = FakeApp("code-oss", "Code - OSS", single_instance=True)
    system = FakeSystem()
    fake_hypr.on_exec = system.kitty.launched
    fake_hypr.proc = tmp_path / "proc"
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(catalog),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=0.5)),
        which=installed,
        system=SystemControl(system, home=home, proc=tmp_path / "proc"),
        terminals=SonuTerminals(tmp_path / "run" / "terminals", system),
    )
    yield Rig(fake_hypr, system, ctx, home, [])


def summaries(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_open_a_project_in_vs_code(rig: Rig) -> None:
    report = await rig.run(
        "open my SONU project in code oss", ("dev.open_project", {"folder": "SONU", "editor": "code oss"})
    )
    assert report.status == "done", summaries(report)
    assert summaries(report) == ["Opened ~/dev_works/SONU in Code - OSS"]
    assert rig.fake.launched[-1] == ["/usr/bin/code-oss", str(rig.home / "dev_works/SONU")]
    assert not rig.confirmations


@pytest.mark.anyio
async def test_the_editor_already_open_is_used(rig: Rig) -> None:
    """VSCodium and Code - OSS are both installed; Code - OSS has a window."""
    rig.fake.windows.append(make_window("0x9000", "code-oss", "Welcome - Code - OSS"))
    report = await rig.run("open SONU in my editor", ("dev.open_project", {"folder": "SONU"}))
    assert rig.fake.launched[-1][0] == "/usr/bin/code-oss"
    assert report.status == "done", summaries(report)


@pytest.mark.anyio
async def test_open_a_file_at_a_line(rig: Rig) -> None:
    report = await rig.run(
        "open planner.py in SONU at line 40 in code oss",
        ("dev.open_project", {"folder": "SONU", "file": "planner.py", "line": 40, "editor": "code oss"}),
    )
    assert report.status == "done", summaries(report)
    planner = rig.home / "dev_works/SONU/src/sonu/core/planner.py"
    assert rig.fake.launched[-1][-2:] == ["--goto", f"{planner}:40"]
    assert summaries(report) == ["Opened planner.py in Code - OSS"]


@pytest.mark.anyio
async def test_two_files_of_that_name_are_asked_about(rig: Rig) -> None:
    report = await rig.run(
        "open the readme in SONU", ("dev.open_project", {"folder": "SONU", "file": "readme"})
    )
    assert report.status == "needs-input"
    choices = report.steps[0].outcome.choices
    assert choices is not None
    assert [o.label for o in choices.options] == ["README.md", "docs/README.md"]


@pytest.mark.anyio
async def test_a_project_and_file_must_be_the_users_words(rig: Rig) -> None:
    report = await rig.run("open my project", ("dev.open_project", {"folder": "SONU"}))
    assert report.status == "failed"
    assert "must be your own words" in summaries(report)[0]
    assert not rig.fake.launched


@pytest.mark.anyio
async def test_run_the_tests_asks_with_the_exact_command_then_says_it_passed(rig: Rig) -> None:
    report = await rig.run("run the tests in SONU", ("dev.run", {"task": "tests", "folder": "SONU"}))
    assert report.status == "done", summaries(report)
    assert rig.system.tasks_run == ["uv run pytest"]
    assert [c.tool for c in rig.confirmations] == ["dev.run"]
    assert "Run uv run pytest in ~/dev_works/SONU (from pyproject.toml)" in rig.confirmations[0].description
    assert summaries(report) == ["uv run pytest in SONU passed (3 passed)"]


@pytest.mark.anyio
async def test_a_failing_command_is_reported_failed_with_its_counts(rig: Rig) -> None:
    rig.system.task_results["uv run pytest"] = (1, "FAILED tests/x.py::y\n=== 2 failed, 118 passed in 9s ===")
    report = await rig.run("run the tests in SONU", ("dev.run", {"task": "tests", "folder": "SONU"}))
    assert report.status == "failed"
    assert summaries(report) == [
        "uv run pytest in SONU failed (exit code 1, 2 failed, 118 passed) — the output is in its window"
    ]
    later = await rig.run("did the tests pass?", ("dev.run_result", {}))
    assert later.status == "done"
    assert summaries(later) == summaries(report)


@pytest.mark.anyio
async def test_a_long_command_is_still_running(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_tools, "RUN_WAIT_S", 0.3)
    rig.system.task_results["uv run pytest"] = (None, "collecting ...")
    report = await rig.run("run the tests in SONU", ("dev.run", {"task": "tests", "folder": "SONU"}))
    assert report.status == "unverified"
    assert "still running in its window" in summaries(report)[0]


@pytest.mark.anyio
async def test_nothing_runs_when_the_user_says_no(rig: Rig) -> None:
    report = await rig.run(
        "run the tests in SONU", ("dev.run", {"task": "tests", "folder": "SONU"}), approve=False
    )
    assert report.status == "declined"
    assert rig.system.tasks_run == []


@pytest.mark.anyio
async def test_a_deploy_is_high_risk(rig: Rig) -> None:
    await rig.run("run deploy in shop-app", ("dev.run", {"task": "deploy", "folder": "shop-app"}))
    assert rig.confirmations[0].risk is Risk.HIGH
    assert "sends something off this computer" in rig.confirmations[0].description


@pytest.mark.anyio
async def test_an_unknown_command_lists_the_projects_own(rig: Rig) -> None:
    report = await rig.run("run the build in shop-app", ("dev.run", {"task": "build", "folder": "shop-app"}))
    assert report.status == "failed"
    assert summaries(report) == ["~/dev_works/shop-app has no 'build' command — it has: test, deploy"]
    assert rig.system.tasks_run == []


@pytest.mark.anyio
async def test_what_changed_is_private(rig: Rig) -> None:
    report = await rig.run("what did I change in SONU?", ("dev.git", {"folder": "SONU"}))
    assert report.status == "done"
    outcome = report.steps[0].outcome
    assert outcome.private
    assert outcome.summary == (
        "SONU (on main, 1 commit not pushed): 2 files changed, +52 -3: src/app.py +12 -3; "
        "src/login.py (new) +40 -0. 1 new untracked: notes.txt"
    )
    commits = await rig.run("my last commits in SONU", ("dev.git", {"folder": "SONU", "what": "commits"}))
    assert commits.steps[0].outcome.summary == (
        "The last commits in SONU: abc1234 Add the login page (2 hours ago); def5678 First commit (1 day ago)"
    )


@pytest.mark.anyio
async def test_a_folder_that_isnt_a_repository(rig: Rig) -> None:
    write(rig.home / "dev_works/blog/package.json", "{}")
    report = await rig.run("what did I change in blog?", ("dev.git", {"folder": "blog"}))
    assert report.status == "failed"
    assert summaries(report) == ["blog isn't a git repository"]


@pytest.mark.anyio
async def test_a_commands_window_isnt_taken_for_claude_code(rig: Rig) -> None:
    """Found while building D65: a command's window stays open, and "what is Claude doing?" read it."""
    await rig.run("run the tests in SONU", ("dev.run", {"task": "tests", "folder": "SONU"}))
    report = await rig.run("what is claude doing?", ("dev.read", {}))
    assert report.status == "failed"
    assert "haven't opened Claude Code" in summaries(report)[0]


@pytest.mark.anyio
async def test_nothing_run_yet(rig: Rig) -> None:
    report = await rig.run("did the tests pass?", ("dev.run_result", {}))
    assert report.status == "failed"
    assert summaries(report) == ["I haven't run anything yet"]


@pytest.mark.anyio
async def test_only_the_last_runs_are_kept(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_tools, "KEEP_RUNS", 2)
    for _ in range(3):
        await rig.run("run the tests in SONU", ("dev.run", {"task": "tests", "folder": "SONU"}))
    folder = rig.ctx.sonu_terminals().folder
    assert len(list(folder.glob("*.run"))) == 2
    assert len(list(folder.glob("*.exit"))) == 2


# ---------------------------------------------------------------------------- dev.terminal
@pytest.mark.anyio
async def test_a_terminal_opens_in_the_folder_and_its_shell_is_checked(rig: Rig) -> None:
    report = await rig.run("open a terminal in dev_works", ("dev.terminal", {"folder": "dev_works"}))
    assert report.status == "done", summaries(report)
    assert rig.fake.launched[-1] == ["kitty", "--directory", str(rig.home / "dev_works")]
    assert summaries(report) == ["Opened kitty in ~/dev_works on workspace 1"]
    assert [c.description for c in report.steps[0].outcome.checks] == [
        "kitty opened",
        "its shell is in ~/dev_works",
    ]


@pytest.mark.anyio
async def test_the_workspace_the_user_named(rig: Rig) -> None:
    report = await rig.run("open a terminal in SONU on workspace 3", ("dev.terminal", {"folder": "SONU"}))
    assert report.status == "done", summaries(report)
    assert summaries(report) == ["Opened kitty in ~/dev_works/SONU on workspace 3"]


@pytest.mark.anyio
async def test_a_terminal_in_the_folder_this_request_made(rig: Rig) -> None:
    report = await rig.run(
        "make a folder called demo in dev_works and open a terminal there",
        ("files.make_folder", {"name": "demo", "inside": "dev_works"}),
        ("dev.terminal", {}),
    )
    assert report.status == "done", summaries(report)
    assert rig.fake.launched[-1] == ["kitty", "--directory", str(rig.home / "dev_works/demo")]


@pytest.mark.anyio
async def test_a_shell_not_seen_in_the_folder_is_unverified(rig: Rig) -> None:
    rig.fake.proc = None  # the shell's folder can't be seen
    report = await rig.run("open a terminal in dev_works", ("dev.terminal", {"folder": "dev_works"}))
    assert report.status == "unverified"
    assert summaries(report) == ["Opened kitty, but I couldn't see its shell start in ~/dev_works"]


@pytest.mark.anyio
async def test_no_folder_no_terminal(rig: Rig) -> None:
    report = await rig.run("open a terminal", ("dev.terminal", {}))
    assert report.status == "failed"
    assert summaries(report) == ["Which folder? Say its name (“… in SONU”)"]
    assert not rig.fake.launched


@pytest.mark.anyio
async def test_the_folder_this_request_made_by_its_name(rig: Rig) -> None:
    """Found on the laptop (dry run): "…and open a terminal there" named the new folder, which a dry run
    doesn't make — the folder this request makes is used when the words name it."""
    report = await rig.run(
        "make a folder called demo in dev_works and open a terminal there",
        ("files.make_folder", {"name": "demo", "inside": "dev_works"}),
        ("dev.terminal", {"folder": "demo"}),
        dry_run=True,
    )
    assert summaries(report) == ["Make the folder ~/dev_works/demo", "Open kitty in ~/dev_works/demo"]
