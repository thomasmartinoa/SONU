"""What's going on inside the laptop (D81): "what's using my CPU?", "why is my fan loud?", "did anything
crash?", "stop that program".

Read-only except stopping a program: SONU finds the program from the user's words among **their own**
processes, never the desktop's core (Hyprland, PipeWire, the shell, SONU itself…), shows what it found, asks
(high risk: unsaved work goes), and asks it to quit (SIGTERM) — never forces it (SIGKILL). Done when its
processes are gone; still running means it may be asking to save, and SONU says so.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.resolver import normalize
from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import ProgramUse
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import not_own_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

TOP = 5
STOP_WAIT_S = 5.0
_MIB = 1024**2
# Never stopped: what the desktop session, its sound, SONU and its models run on.
PROTECTED = {
    "hyprland",
    "xwayland",
    "systemd",
    "dbus-daemon",
    "dbus-broker",
    "dbus-broker-launch",
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "quickshell",
    "qs",
    "xdg-desktop-portal",
    "xdg-desktop-portal-hyprland",
    "xdg-desktop-portal-gtk",
    "polkit-gnome-authentication-agent-1",
    "hyprpolkitagent",
    "gnome-keyring-daemon",
    "ssh-agent",
    "gpg-agent",
    "ollama",
    "sonu",
    "sonud",
    "uwsm",
    "sddm",
    "greetd",
}


def _mib(byte_count: int) -> str:
    return f"{byte_count / 1024**3:.1f} GB" if byte_count >= 1024**3 else f"{byte_count // _MIB} MB"


def _matches(use: ProgramUse, words: str) -> bool:
    want = normalize(words).replace(" ", "")
    have = normalize(use.program).replace(" ", "")
    # The program has the user's words ("chrom" → chromium) — not the other way round: found on the laptop,
    # "kill sonu-nonexistent-program" matched SONU's own process.
    return bool(want) and (want == have or (len(want) >= 3 and want in have))


# ---------------------------------------------------------------------------- system.processes
class ProcessArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: Literal["cpu", "memory", "gpu"] = Field(default="cpu", description="cpu, memory or gpu")


class Processes(Tool):
    name = "system.processes"
    description = (
        "Which programs are using the CPU, memory or GPU, with the fans and temperature: 'what's using my "
        "CPU?', 'why is my fan loud?', 'what's eating my memory?' (read-only)."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = ProcessArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        by = cast(ProcessArgs, args).by
        return Prepared(f"Look at what's using the {'GPU' if by == 'gpu' else by}", by)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        by = cast(str, prepared.payload)
        system = ctx.system
        uses = await system.processes()
        parts: list[str] = []
        if by == "memory":
            top = sorted(uses, key=lambda u: -u.memory)[:TOP]
            parts.append("Memory: " + "; ".join(f"{u.program} {_mib(u.memory)}" for u in top))
        elif by == "gpu":
            try:
                gpu = await system.gpu_programs() if system.available("nvidia-smi") else []
            except (CommandError, ProgramMissing):
                gpu = []
            parts.append(
                "GPU memory: " + "; ".join(f"{name} {mib} MB" for name, mib in gpu)
                if gpu
                else "Nothing on the GPU"
            )
        else:
            busy = [u for u in uses if u.cpu >= 1][:TOP]
            parts.append(
                "CPU: " + "; ".join(f"{u.program} {u.cpu:.0f}%" for u in busy)
                if busy
                else "CPU: nothing busy"
            )
        warm = []
        if (temp := system.cpu_temperature()) is not None:
            warm.append(f"CPU {temp:.0f}°C")
        if fans := system.fans():
            warm.append("fans " + " / ".join(f"{rpm} RPM" for rpm in fans))
        if warm:
            parts.append(", ".join(warm))
        checks = (Check("the running programs were read", bool(uses)),)
        if not uses:
            return ToolOutcome(StepStatus.FAILED, "Couldn't read the running programs", checks)
        # Program names say what the user is doing: shown to them, kept out of models and the log.
        return ToolOutcome(StepStatus.DONE, " · ".join(parts), checks, private=True)


# ---------------------------------------------------------------------------- system.problems
class Problems(Tool):
    name = "system.problems"
    description = (
        "Whether anything went wrong on the laptop: failed services, programs that crashed today, errors in "
        "the last hour ('did anything crash?', 'any errors?') — read-only."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        return Prepared("Look for failed services, crashes and errors", None)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        system = ctx.system
        failed: list[str] = []
        crashed: list[tuple[float, str, int]] = []
        errors: list[tuple[str, str]] = []
        with_programs = []
        try:
            if system.available("systemctl"):
                failed = await system.failed_units()
                with_programs.append("services")
            if system.available("coredumpctl"):
                crashed = await system.crashes("-1d")
                with_programs.append("crashes")
            if system.available("journalctl"):
                errors = await system.journal_errors("-1h")
                with_programs.append("errors")
        except (CommandError, ProgramMissing) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read the system's logs: {exc}")
        if not with_programs:
            return ToolOutcome(StepStatus.FAILED, "systemctl, coredumpctl and journalctl aren't here")
        parts = [f"Failed services: {', '.join(failed)}" if failed else "No failed services"]
        if crashed:
            latest = sorted(crashed, key=lambda c: -c[0])[:TOP]
            parts.append(
                "Crashed today: "
                + "; ".join(
                    f"{program} at {datetime.fromtimestamp(when):%H:%M}" for when, program, _ in latest
                )
            )
        else:
            parts.append("nothing crashed today")
        if errors:
            counts: dict[str, int] = {}
            for who, _ in errors:
                counts[who] = counts.get(who, 0) + 1
            busiest = sorted(counts.items(), key=lambda i: -i[1])[:TOP]
            parts.append("errors in the last hour: " + "; ".join(f"{who} {n}" for who, n in busiest))
            parts.append(f"the latest: {errors[-1][0]}: {' '.join(errors[-1][1].split())[:160]}")
        else:
            parts.append("no errors in the last hour")
        checks = (Check("the system's logs were read", True, {"read": with_programs}),)
        # Log lines can carry paths and names: shown to the user, never given to a model or the log.
        return ToolOutcome(StepStatus.DONE, " · ".join(parts), checks, private=True)


# ---------------------------------------------------------------------------- system.stop_program
class StopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: str = Field(
        min_length=1, max_length=80, description="the user's words for the program ('chromium')"
    )


class StopProgram(Tool):
    name = "system.stop_program"
    description = (
        "Ask a running program of the user's to quit (kill / force quit / stop a program, including ones "
        "with no window) — asks first; never the desktop's own programs. Closing a window is windows.close."
    )
    risk = Risk.HIGH  # unsaved work in it goes
    effects = frozenset({Effect.PROCESS_STOP})
    Args = StopArgs
    grounded = ("program",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(StopArgs, args)
        if problem := not_own_words(ctx, a.program):
            return problem
        mine = [u for u in await ctx.system.processes(sample_s=0.3) if u.uid == os.getuid()]
        if choice:
            found = [u for u in mine if u.program == choice]
        elif any(normalize(u.program) == normalize(a.program) for u in mine):
            found = [u for u in mine if normalize(u.program) == normalize(a.program)]
        else:
            found = [u for u in mine if _matches(u, a.program)]
        if not found:
            return ToolOutcome(StepStatus.FAILED, f"No program of yours called '{a.program}' is running")
        if len(found) > 1:
            options = tuple(
                Choice(f"{u.program} ({u.cpu:.0f}% CPU, {_mib(u.memory)})", u.program) for u in found[:6]
            )
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"several programs match '{a.program}'",
                question=f"Which '{a.program}'?",
                choices=Choices(f"Which '{a.program}'?", options),
            )
        target = found[0]
        own = {os.getpid(), os.getppid()}
        if normalize(target.program) in PROTECTED or own & set(target.pids):
            return ToolOutcome(
                StepStatus.FAILED, f"SONU won't stop {target.program}: the desktop (or SONU) runs on it"
            )
        count = f"{len(target.pids)} processes" if len(target.pids) != 1 else "1 process"
        return Prepared(
            f"Ask {target.program} to quit ({count}, {target.cpu:.0f}% CPU, {_mib(target.memory)}) — unsaved "
            "work in it is lost",
            target,
            spoken=f"stop {target.program}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        target = cast(ProgramUse, prepared.payload)
        asked = ctx.system.stop_processes(list(target.pids))
        if not asked:
            return ToolOutcome(StepStatus.FAILED, f"{target.program} had already stopped")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_WAIT_S
        left = ctx.system.running(asked)
        while left and loop.time() < deadline:
            await asyncio.sleep(0.2)
            left = ctx.system.running(asked)
        gone = Check(f"{target.program} has quit", not left)
        if left:
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Asked {target.program} to quit, but it's still running (it may be asking to save) — SONU "
                "doesn't force it",
                (gone,),
            )
        return ToolOutcome(StepStatus.DONE, f"{target.program} has quit", (gone,))


HEALTH_TOOLS: tuple[Tool, ...] = (Processes(), Problems(), StopProgram())
