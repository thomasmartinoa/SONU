""" "Install Spotify" — packages from the official repos, then the AUR (M4.5, D54).

SONU finds the package: the name the user said if the official repos have it; otherwise the user
picks from SONU's own list (the official matches, then the AUR's exact name). It explains what it is
and where it comes from — an AUR package is built on this laptop from a script anyone can submit —
and asks (high risk; by voice the OK comes from the screen). The install runs in a terminal SONU
opens, where the user can watch it: ``pkexec`` asks for the password with the system's own dialog,
only at that moment; SONU never runs sudo or sees the password. It's done only when ``pacman -Q``
shows the package.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.hyprland.errors import HyprlandError
from sonu.system.commands import CommandError
from sonu.system.packages import Package, Packages, candidates, rank, valid_name
from sonu.tools.base import (
    Choice,
    Choices,
    Effect,
    Prepared,
    Risk,
    StepStatus,
    Tool,
    ToolOutcome,
    outcome_from_checks,
)
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

OFFICIAL_TIMEOUT_S = 20 * 60
AUR_TIMEOUT_S = 60 * 60
POLL_S = 1.0  # how often the install is looked at
# What a failed or cancelled install prints (pkexec, pacman, makepkg/yay).
_FAILED = (
    "Request dismissed",
    "Not authorized",
    "Error executing command as another user",
    "error: target not found",
    "error: failed to",
    "==> ERROR",
    "Unable to install",
)


def _already(asked: str, name: str, version: str) -> ToolOutcome:
    said = (
        f"{name} is already installed ({version})"
        if name == asked
        else (f"{asked} is already there — {name} {version} provides it")
    )
    return ToolOutcome(StepStatus.DONE, said, (Check("pacman says it's installed", True, {"package": name}),))


def _describe(package: Package) -> str:
    what = package.description or "no description"
    if package.aur:
        votes = f", {package.votes} votes" if package.votes is not None else ""
        return f"{package.name} {package.version} from the AUR ({what}{votes})"
    size = f", {package.size} to download" if package.size else ""
    return f"{package.name} {package.version} from the official {package.repo} repo ({what}{size})"


class InstallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80, description="what to install, in the user's words")


class Install(Tool):
    name = "packages.install"
    description = (
        "Install software by name (official Arch repos first, then the AUR). The system asks for the "
        "password itself; it runs in a window the user can watch."
    )
    risk = Risk.HIGH
    effects = frozenset({Effect.INSTALL})
    Args = InstallArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        words = cast(InstallArgs, args).name
        if missing := ungrounded_words(words, ctx.user_words or ctx.request_text):
            return ToolOutcome(
                StepStatus.FAILED, f"What to install must be your words ({', '.join(missing)} isn't)"
            )
        if not ctx.which("pacman") or not ctx.which("pkexec"):
            return ToolOutcome(StepStatus.FAILED, "Installing needs pacman and pkexec (polkit)")
        packages = Packages(ctx.system.runner)
        for name in candidates(words) if not choice else []:
            if found := await packages.installed(name):
                return _already(name, *found)
        package = await self._find(packages, words, ctx, choice)
        if isinstance(package, ToolOutcome):
            return package
        if found := await packages.installed(package.name):
            return _already(package.name, *found)
        warning = (
            " — it's built on this laptop from a script anyone can submit; install it only if you trust it"
            if package.aur
            else ""
        )
        return Prepared(
            f"Install {_describe(package)}{warning}. Your password is asked by the system itself.",
            package,
            spoken=f"install {package.name}{' from the AUR' if package.aur else ''}",
        )

    async def _find(
        self, packages: Packages, words: str, ctx: ToolContext, choice: str | None
    ) -> Package | ToolOutcome:
        if choice:
            repo, _, name = choice.partition("/")
            found = await (packages.aur(name) if repo == "aur" else packages.official(name))
            return found or ToolOutcome(StepStatus.FAILED, f"{name} can't be found anymore")
        for name in candidates(words):
            if found := await packages.official(name):
                return found  # the name the user said, in the official repos
        official = rank(await packages.search_official(words), words)[:5]
        aur_exact: Package | None = None
        for name in candidates(words):
            if aur_exact := await packages.aur(name):
                break
        options = [*official, *([aur_exact] if aur_exact else [])]
        if not options:
            options = rank(await packages.search_aur(words), words)[:4]
        if not options:
            return ToolOutcome(StepStatus.FAILED, f"I couldn't find a package for '{words}'")
        if len(options) == 1 and not options[0].aur:
            return options[0]
        marks = [" (installed)" if await packages.installed(p.name) else "" for p in options]
        offered = tuple(
            Choice(
                f"{p.name}{mark} — {'AUR' if p.aur else p.repo}: {p.description[:70]}",
                f"{p.repo}/{p.name}",
            )
            for p, mark in zip(options, marks, strict=True)
        )
        question = f"Which package for '{words}'?"
        return ToolOutcome(
            StepStatus.NEEDS_INPUT,
            f"{len(options)} packages could be '{words}'",
            question=question,
            choices=Choices(question, offered),
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        package = cast(Package, prepared.payload)
        if not valid_name(package.name):
            return ToolOutcome(StepStatus.FAILED, f"'{package.name}' isn't a package name")
        argv: tuple[str, ...]
        if package.aur:
            argv = ("yay", "-S", "--needed", "--noconfirm", "--sudo", "pkexec", "--answerdiff", "None",
                    "--answerclean", "None", "--removemake", "--", package.name)  # fmt: skip
        else:
            argv = ("pkexec", "pacman", "-S", "--needed", "--noconfirm", "--", package.name)
        terminals = ctx.sonu_terminals()
        terminal, launch = terminals.new(
            ctx.system.home, argv[0], argv[1:], label=f"Install {package.name}", hold=True
        )
        try:
            await ctx.desktop.launch(launch, None)
        except HyprlandError as exc:
            terminals.forget(terminal)
            return ToolOutcome(StepStatus.FAILED, f"Couldn't open the install window: {exc}")
        packages = Packages(ctx.system.runner)
        deadline = time.monotonic() + (AUR_TIMEOUT_S if package.aur else OFFICIAL_TIMEOUT_S)
        version: str | None = None
        problem = ""
        seen_window = False
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            if found := await packages.installed(package.name):
                version = found[1]
                break
            try:
                screen = await terminals.screen(terminal)
                seen_window = True
            except CommandError:
                if seen_window:
                    problem = "the install window was closed before it finished"
                    break
                continue
            if marker := next((m for m in _FAILED if m in screen), None):
                problem = "the password prompt was cancelled" if "dismissed" in marker else marker
                break
        if version:
            await asyncio.sleep(POLL_S)  # let pacman finish writing its output
            with contextlib.suppress(CommandError):
                await terminals.close(terminal)
        where = "the AUR" if package.aur else f"the {package.repo} repo"
        return outcome_from_checks(
            [Check("pacman says it's installed", version is not None, {"version": version})],
            f"Installed {package.name} {version} from {where}",
            f"{package.name} wasn't installed: {problem or 'it took too long'} "
            "(the window shows what happened)",
        )


PACKAGE_TOOLS: tuple[Tool, ...] = (Install(),)
