"""What's going on inside the laptop (D81): programs by CPU / memory / GPU, fans, crashes and errors, and
asking a program to quit."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import Settings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.system.control import ProcessSample, _program_name, program_use
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_system import FakeSystem
from sonu.tools import health_tools
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


def test_a_programs_processes_count_together() -> None:
    def sample(pid: int, program: str, ticks: int, rss: int = 100) -> ProcessSample:
        return ProcessSample(pid, program, ticks, rss, 1000)

    first = {
        1: sample(1, "zen", 100),
        2: sample(2, "zen", 50),
        3: sample(3, "kitty", 10),
        9: sample(9, "old", 5),
    }
    second = {
        1: sample(1, "zen", 160),  # 60 ticks
        2: sample(2, "zen", 90),  # 40 ticks
        3: sample(3, "kitty", 15),  # 5 ticks
        4: sample(4, "zen", 999),  # started in between: from zero
    }
    uses = program_use(first, second, elapsed_s=1.0, ticks_per_s=100)
    assert [(u.program, round(u.cpu), u.pids) for u in uses] == [("zen", 100, (1, 2, 4)), ("kitty", 5, (3,))]
    assert uses[0].memory == 300


def test_program_names() -> None:
    """Found on the laptop: Claude Code's program file is named after its version."""
    assert _program_name("2.1.295", "claude") == "claude"
    assert _program_name("python3.12", "python3") == "python"
    assert _program_name("zen-bin", "zen") == "zen"
    assert _program_name("chromium", "chromium") == "chromium"
    assert _program_name("llama-server", "llama-server") == "ollama"


def fake_proc(root: Path, pid: int, comm: str, ticks: int, pages: int, exe: str | None = None) -> None:
    folder = root / str(pid)
    folder.mkdir(parents=True)
    (folder / "stat").write_text(f"{pid} ({comm}) S 1 1 1 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0 0 0 0\n")
    (folder / "statm").write_text(f"1000 {pages} 0 0 0 0 0\n")
    if exe:
        (folder / "exe").symlink_to(exe)


class Rig:
    def __init__(self, ctx: ToolContext, system: FakeSystem, home: Path) -> None:
        self.ctx, self.system, self.home = ctx, system, home
        self.asked: list[ConfirmRequest] = []

    async def run(self, request: str, tool: str, args: dict[str, Any], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.asked.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan((tool, args))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    proc, sys_dir = tmp_path / "proc", tmp_path / "sys"
    fake_proc(proc, 100, "chromium", 500, 50_000, "/usr/lib/chromium/chromium")
    fake_proc(proc, 101, "pipewire", 10, 2_000, "/usr/bin/pipewire")
    fans = sys_dir / "class" / "hwmon" / "hwmon5"
    fans.mkdir(parents=True)
    (fans / "name").write_text("acer\n")
    (fans / "fan1_input").write_text("2800\n")
    (fans / "fan2_input").write_text("3150\n")
    system = FakeSystem(
        failed_units=["grub-btrfsd.service"],
        crashed=[(1_790_000_000.0, "/usr/bin/quickshell", 11)],
        errors=[("grub-btrfsd", "Couldn't find /.snapshots"), ("kernel", "ACPI error")],
    )
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(AppCatalog(())),
        Settings(),
        which=lambda name: None,
        system=SystemControl(system, home=tmp_path, proc=proc, sys=sys_dir),
    )
    yield Rig(ctx, system, tmp_path)


def lines(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_whats_using_the_memory_and_why_the_fan_is_loud(rig: Rig) -> None:
    report = await rig.run("what's eating my memory?", "system.processes", {"by": "memory"})
    outcome = report.steps[0].outcome
    assert report.status == "done" and outcome.private
    assert outcome.summary.startswith("Memory: chromium ")
    assert "fans 2800 RPM / 3150 RPM" in outcome.summary
    report = await rig.run("why is my fan loud?", "system.processes", {})
    assert lines(report)[0].startswith("CPU: nothing busy")  # the fake /proc doesn't move
    report = await rig.run("what's using my gpu?", "system.processes", {"by": "gpu"})
    rig.ctx.which = lambda name: None
    assert lines(report)[0].startswith("GPU memory: ollama 3600 MB")


@pytest.mark.anyio
async def test_did_anything_crash(rig: Rig) -> None:
    report = await rig.run("did anything crash?", "system.problems", {})
    summary = lines(report)[0]
    assert report.status == "done" and report.steps[0].outcome.private
    assert summary.startswith("Failed services: grub-btrfsd.service · Crashed today: quickshell at ")
    assert "errors in the last hour: grub-btrfsd 1; kernel 1" in summary
    assert summary.endswith("the latest: kernel: ACPI error")


@pytest.mark.anyio
async def test_the_desktops_own_programs_are_never_stopped(rig: Rig) -> None:
    report = await rig.run("kill pipewire", "system.stop_program", {"program": "pipewire"})
    assert lines(report) == ["SONU won't stop pipewire: the desktop (or SONU) runs on it"]
    assert rig.asked == []
    report = await rig.run("kill the browser", "system.stop_program", {"program": "my banking app"})
    assert report.status == "failed" and rig.asked == []  # not the user's words


@pytest.mark.anyio
async def test_with_a_fake_proc_nothing_is_signalled(rig: Rig) -> None:
    """A made-up pid may be a real process on the machine running the tests: the fake system only records."""
    rig.system.proc = rig.ctx.system.proc
    report = await rig.run("kill chromium", "system.stop_program", {"program": "chromium"})
    assert lines(report) == ["chromium has quit"]
    assert rig.system.signals == [(100, signal.SIGTERM)]


# ---------------------------------------------------------------------------- real processes (our own)
@pytest.fixture
def programs(tmp_path: Path) -> Iterator[dict[str, subprocess.Popen[bytes]]]:
    """Programs with names nothing else has (copies of bash): the real /proc, our own children. Not a copy of
    sleep: on CI `sleep` is a multi-call binary (uutils coreutils) that exits 1 under another name."""
    nap, stubborn = tmp_path / "sonutestnap", tmp_path / "sonustubborn"
    shutil.copy(shutil.which("bash") or "/usr/bin/bash", nap)
    shutil.copy(shutil.which("bash") or "/usr/bin/bash", stubborn)
    started = {
        "nap": subprocess.Popen([str(nap), "-c", "while :; do sleep 0.2; done"]),
        "stubborn": subprocess.Popen([str(stubborn), "-c", 'trap "" TERM; while :; do sleep 0.2; done']),
    }
    yield started
    for process in started.values():
        if process.poll() is None:
            process.kill()  # the test's own leftovers (the stubborn one ignores SIGTERM)
        process.wait(5)


@pytest.mark.anyio
async def test_stop_a_program_asks_and_checks(
    rig: Rig, programs: dict[str, subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_tools, "STOP_WAIT_S", 1.0)
    rig.ctx.system.proc = Path("/proc")  # the real processes: only ours have these names
    rig.ctx.system.kill = os.kill  # really signalled: they're this test's own children
    report = await rig.run(
        "kill sonutestnap", "system.stop_program", {"program": "sonutestnap"}, approve=False
    )
    assert report.status == "declined" and programs["nap"].poll() is None
    [asked] = rig.asked
    assert asked.risk is Risk.HIGH and asked.description.startswith("Ask sonutestnap to quit (1 process,")
    report = await rig.run("kill sonutestnap", "system.stop_program", {"program": "sonutestnap"})
    assert lines(report) == ["sonutestnap has quit"]
    await asyncio.sleep(0.1)
    assert programs["nap"].wait(2) == -signal.SIGTERM  # asked, not forced
    report = await rig.run("kill sonustubborn", "system.stop_program", {"program": "sonustubborn"})
    assert report.status == "unverified"
    assert lines(report)[0].startswith("Asked sonustubborn to quit, but it's still running")
    assert programs["stubborn"].poll() is None  # SONU never forces it


def test_a_program_has_the_users_words_not_the_other_way_round() -> None:
    """Found on the laptop (dry run): "kill sonu-nonexistent-program" matched SONU's own process."""
    from sonu.system.control import ProgramUse
    from sonu.tools.health_tools import _matches

    sonu = ProgramUse("sonu", 1.0, 1, (1,), 1000)
    chromium = ProgramUse("chromium", 1.0, 1, (2,), 1000)
    assert not _matches(sonu, "sonu-nonexistent-program")
    assert _matches(chromium, "chrom") and _matches(chromium, "Chromium")
