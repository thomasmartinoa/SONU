"""The eval runner: real planner model + real engine + fake desktop → a measurable score.

Every case starts from a fresh fake Hyprland with the sample apps installed, runs the request
through the complete SONU loop with confirmations auto-approved (and recorded), then checks the
final desktop state against the case's expectations.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.planner import LLMPlanner
from sonu.core.report import TaskReport
from sonu.desktop.apps.catalog import AppCatalog
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.llm.chat import ChatModel
from sonu.policy.engine import Policy
from sonu.system.control import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_system import FakeSystem
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

# How the sample apps behave on the fake desktop (argv[0] basename → behaviour).
EVAL_APPS: dict[str, FakeApp] = {
    "code-oss": FakeApp("code-oss", "Welcome - Code - OSS", single_instance=True),
    "codium": FakeApp("VSCodium", "Welcome - VSCodium"),
    "chromium": FakeApp("chromium", "New Tab - Chromium"),
    "zen-browser": FakeApp("zen", "New Tab - Zen Browser"),
    "kitty": FakeApp("kitty", "zsh"),
    "thunar": FakeApp("Thunar", "Home - Thunar"),
    "whatsapp-desktop": FakeApp("WhatsApp Desktop", "WhatsApp"),
    "blueman-manager": FakeApp("blueman-manager", "Bluetooth Devices"),
}
ROLE_DEFAULTS = {"browser": "zen-browser", "terminal": "kitty"}


# FakeSystem properties an eval may check besides its fields.
_SYSTEM_PROPERTIES = {
    "brightness",
    "bt_connected",
    "videos_playing",
    "reminders",
    "night_light",
    "keep_awake",
    "do_not_disturb",
    "screenshots",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CaseWindow(_Model):
    window_class: str = Field(alias="class")
    title: str = ""
    workspace: int = 1
    focused: bool = False


class ExpectWindow(_Model):
    window_class: str = Field(alias="class")
    workspace: int | None = None
    title_contains: str | None = None
    count: int | None = None


class EvalCase(_Model):
    id: str
    request: str
    notes: str = ""
    windows: list[CaseWindow] = Field(default_factory=list)
    expect_status: list[str]
    expect_windows: list[ExpectWindow] = Field(default_factory=list)
    expect_absent: list[str] = Field(default_factory=list)
    expect_shown: int | None = None
    forbid_tools: list[str] = Field(default_factory=list)
    # True: the plan must list something it can't do; False: it must not (unset: either).
    expect_unsupported: bool | None = None
    # System settings: FakeSystem fields to start from, and what they must be at the end. A number
    # matches within 0.011; {"below" = x} / {"above" = x} compare. "brightness" is the screen's %.
    system: dict[str, Any] = Field(default_factory=dict)
    expect_system: dict[str, Any] = Field(default_factory=dict)
    battery: int | None = None  # a battery at this percent (discharging)
    expect_confirmed: list[str] = Field(default_factory=list)  # tools that must have asked first
    expect_reply_contains: str | None = None  # text the report must contain

    @field_validator("system", "expect_system")
    @classmethod
    def _known_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - set(FakeSystem.__dataclass_fields__) - _SYSTEM_PROPERTIES
        if unknown:
            raise ValueError(f"unknown system fields: {sorted(unknown)}")
        return value

    require_tools: list[str] = Field(default_factory=list)


class _CaseFile(_Model):
    case: list[EvalCase]


def load_cases(path: Path | None = None) -> list[EvalCase]:
    if path is None:
        text = resources.files("sonu.evals").joinpath("cases.toml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    cases = _CaseFile.model_validate(tomllib.loads(text)).case
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("eval case ids must be unique")
    return cases


@dataclass(slots=True)
class CaseResult:
    case_id: str
    passed: bool
    status: str
    failures: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    planning_s: float = 0.0
    total_s: float = 0.0
    attempts: int = 0
    confirmations: int = 0
    # What the model planned (for debugging a failure): kind, message, steps with args, unsupported.
    plan: dict[str, Any] = field(default_factory=dict)


def _system_failures(case: EvalCase, system: FakeSystem) -> list[str]:
    failures = []
    for name, wanted in case.expect_system.items():
        actual = getattr(system, name)
        if isinstance(wanted, dict):
            ok = ("below" not in wanted or actual < wanted["below"]) and (
                "above" not in wanted or actual > wanted["above"]
            )
        elif isinstance(wanted, float | int) and not isinstance(wanted, bool):
            ok = abs(actual - wanted) <= 0.011
        else:
            ok = actual == wanted
        if not ok:
            failures.append(f"{name} should be {wanted}, is {actual}")
    return failures


def check_expectations(
    case: EvalCase,
    report: TaskReport,
    fake: FakeHyprland,
    system: FakeSystem | None = None,
    confirmed: list[str] | None = None,
) -> list[str]:
    failures: list[str] = []
    if system is not None:
        failures.extend(_system_failures(case, system))
    failures.extend(
        f"{tool} should have asked first" for tool in case.expect_confirmed if tool not in (confirmed or [])
    )
    if case.expect_reply_contains and case.expect_reply_contains not in "\n".join(report.lines()):
        failures.append(f"the reply should mention {case.expect_reply_contains!r}")
    if report.status not in case.expect_status:
        detail = report.error or "; ".join(report.lines())[:200]
        failures.append(f"status {report.status!r} not in {case.expect_status} ({detail})")
    for expected in case.expect_windows:
        matches = [
            w
            for w in fake.windows
            if w["class"] == expected.window_class
            and (expected.workspace is None or w["workspace"]["id"] == expected.workspace)
            and (
                expected.title_contains is None or expected.title_contains.casefold() in w["title"].casefold()
            )
        ]
        wanted = expected.count
        if (wanted is None and not matches) or (wanted is not None and len(matches) != wanted):
            where = f" on workspace {expected.workspace}" if expected.workspace is not None else ""
            failures.append(
                f"expected {wanted or 'a'} {expected.window_class} window{where}, found {len(matches)}"
            )
    for window_class in case.expect_absent:
        if fake.windows_of_class(window_class):
            failures.append(f"{window_class} should not be open")
    if case.expect_shown is not None and fake.active_workspace != case.expect_shown:
        failures.append(f"workspace {case.expect_shown} should be shown, not {fake.active_workspace}")
    used = [record.tool for record in report.steps]
    failures.extend(f"must not use {tool}" for tool in case.forbid_tools if tool in used)
    unsupported = report.plan.unsupported if report.plan else ()
    if case.expect_unsupported is True and not unsupported:
        failures.append("should say which part it can't do (unsupported is empty)")
    if case.expect_unsupported is False and unsupported:
        failures.append(f"claimed it can't do: {'; '.join(unsupported)}")
    failures.extend(f"should use {tool}" for tool in case.require_tools if tool not in used)
    return failures


async def run_case(case: EvalCase, model: ChatModel, workdir: Path) -> CaseResult:
    runtime_dir = Path(tempfile.mkdtemp(prefix="sonu-eval-", dir="/tmp"))  # short: socket path limit
    started = time.monotonic()
    try:
        async with FakeHyprland(runtime_dir, signature="eval") as fake:
            fake.apps.update({name: FakeApp(**vars(app)) for name, app in EVAL_APPS.items()})
            focused_address: str | None = None
            for index, window in enumerate(case.windows):
                address = f"0x{0x7000 + index:x}"
                fake.windows.append(
                    make_window(address, window.window_class, window.title, workspace_id=window.workspace)
                )
                if window.focused:
                    focused_address = address
                    fake.monitors[0]["activeWorkspace"] = {
                        "id": window.workspace,
                        "name": str(window.workspace),
                    }
            fake.active_address = focused_address

            apps_home = workdir / "share"
            write_desktop_files(apps_home / "applications")
            env = {
                "HOME": str(workdir),
                "XDG_DATA_HOME": str(apps_home),
                "XDG_DATA_DIRS": str(workdir / "none"),
                "XDG_CURRENT_DESKTOP": "Hyprland",
            }
            catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
            desktop = await HyprlandDesktop.connect(fake.instance)
            system = FakeSystem(**case.system)
            fake.on_url = system.page_opened
            sys_dir = workdir / "sys" / "class" / "thermal" / "thermal_zone0"
            sys_dir.mkdir(parents=True, exist_ok=True)
            (sys_dir / "type").write_text("x86_pkg_temp\n")
            (sys_dir / "temp").write_text("61000\n")
            power_supply = workdir / "power_supply"
            if case.battery is not None:
                (power_supply / "BAT1").mkdir(parents=True, exist_ok=True)
                (power_supply / "BAT1" / "capacity").write_text(f"{case.battery}\n")
                (power_supply / "BAT1" / "status").write_text("Discharging\n")
            settings = Settings(apps=AppsSettings(launch_timeout_s=3.0), verify=VerifySettings(timeout_s=2.0))
            ctx = ToolContext(
                desktop,
                AppResolver(catalog, role_defaults=ROLE_DEFAULTS),
                settings,
                default_terminal="kitty",
                which=lambda name: f"/usr/bin/{name}",
                ancestors=frozenset,
                system=SystemControl(system, power_supply=power_supply, sys=workdir / "sys", home=workdir),
            )
            engine = Engine(
                planner=LLMPlanner(model),
                registry=ToolRegistry(),
                ctx=ctx,
                policy=Policy("confirm-risky"),
            )
            confirmations: list[ConfirmRequest] = []

            async def approve(request: ConfirmRequest) -> bool:
                confirmations.append(request)
                return True

            report = await engine.run(case.request, confirm=approve)
            failures = check_expectations(case, report, fake, system, [c.tool for c in confirmations])
            return CaseResult(
                case_id=case.id,
                passed=not failures,
                status=report.status,
                failures=failures,
                tools=[record.tool for record in report.steps],
                planning_s=report.plan.duration_s if report.plan else 0.0,
                total_s=time.monotonic() - started,
                attempts=report.plan.attempts if report.plan else 0,
                confirmations=len(confirmations),
                plan={}
                if report.plan is None
                else {
                    "kind": report.plan.kind,
                    "message": report.plan.message,
                    "steps": [{"tool": s.tool, "args": s.args} for s in report.plan.steps],
                    "unsupported": list(report.plan.unsupported),
                },
            )
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


@dataclass(slots=True)
class EvalSummary:
    model: str
    results: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def score(self) -> float:
        return self.passed / len(self.results) if self.results else 0.0

    @property
    def mean_planning_s(self) -> float:
        timed = [r.planning_s for r in self.results if r.planning_s]
        return sum(timed) / len(timed) if timed else 0.0


async def run_eval(cases: list[EvalCase], model: ChatModel, workdir: Path) -> EvalSummary:
    results = []
    for case in cases:
        case_dir = workdir / case.id
        case_dir.mkdir(parents=True, exist_ok=True)
        results.append(await run_case(case, model, case_dir))
    return EvalSummary(model=model.name, results=results)
