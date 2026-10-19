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
from collections.abc import Awaitable, Callable
from pathlib import Path
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
    "AUTHENTICATION FAILED",
    "Authentication failure",
)
POLKIT_TTY = Path(__file__).parents[1] / "integrations" / "polkit_tty.py"
SYSTEM_PYTHON = "/usr/bin/python3"


def _asks_password(screen: str) -> bool:
    """The text agent in SONU's install window waits for the password ("Password: ")."""
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return bool(lines) and lines[-1].endswith("Password:")


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


async def _in_window(
    ctx: ToolContext,
    argv: tuple[str, ...],
    label: str,
    *,
    finished: Callable[[], Awaitable[object]],
    timeout_s: float,
    verb: str,
) -> str | None:
    """Run ``argv`` (pkexec …) in a SONU window the user can watch, until ``finished()`` is true:
    None, and the window is closed; otherwise what went wrong (the window stays open to read)."""
    by_voice = ctx.settings.voice.password_by_voice
    if by_voice:  # the prompt in SONU's own window: typed there, or said (D57)
        argv = (SYSTEM_PYTHON, str(POLKIT_TTY), *argv)
    terminals = ctx.sonu_terminals()
    terminal, launch = terminals.new(ctx.system.home, argv[0], argv[1:], label=label, hold=True)
    try:
        await ctx.desktop.launch(launch, None)
    except HyprlandError as exc:
        terminals.forget(terminal)
        return f"couldn't open the window: {exc}"
    deadline = time.monotonic() + timeout_s
    seen_window = asked = spoken = False
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        if await finished():
            await asyncio.sleep(POLL_S)  # let pacman finish writing its output
            with contextlib.suppress(CommandError):
                await terminals.close(terminal)
            return None
        try:
            screen = await terminals.screen(terminal)
            seen_window = True
        except CommandError:
            if seen_window:
                return "the window was closed before it finished"
            continue
        if by_voice and not asked and ctx.ask_secret is not None and _asks_password(screen):
            asked = True
            secret = await ctx.ask_secret("Say your password, or type it in the window.")
            if secret:
                with contextlib.suppress(CommandError):  # never shown, logged or kept
                    await terminals.type(terminal, secret, paste=False)
                    await terminals.keys(terminal, "Enter")
                spoken = True
            del secret
            continue
        if marker := next((m for m in _FAILED if m in screen), None):
            if spoken and "AUTH" in marker.upper():
                return f"the spoken password didn't work — {verb} it again and type the password"
            return "the password prompt was cancelled" if "dismissed" in marker else marker
        if by_voice and "[sonu] finished" in screen:  # polkit_tty.py ended, and it isn't done
            return None if await finished() else "it stopped before it finished (cancelled?)"
    return "it took too long"


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
        packages = Packages(ctx.system.runner)
        argv: tuple[str, ...]
        if package.aur:
            argv = ("yay", "-S", "--needed", "--noconfirm", "--sudo", "pkexec", "--answerdiff", "None",
                    "--answerclean", "None", "--removemake", "--", package.name)  # fmt: skip
        else:
            argv = ("pkexec", "pacman", "-S", "--needed", "--noconfirm", "--", package.name)
        problem = await _in_window(
            ctx,
            argv,
            f"Install {package.name}",
            finished=lambda: packages.installed(package.name),
            timeout_s=AUR_TIMEOUT_S if package.aur else OFFICIAL_TIMEOUT_S,
            verb="install",
        )
        found = await packages.installed(package.name) if problem is None else None
        version = found[1] if found else None
        where = "the AUR" if package.aur else f"the {package.repo} repo"
        return outcome_from_checks(
            [Check("pacman says it's installed", version is not None, {"version": version})],
            f"Installed {package.name} {version} from {where}",
            f"{package.name} wasn't installed: {problem or 'pacman does not list it'} "
            "(the window shows what happened)",
        )


class CheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80, description="the software, in the user's words")


class CheckInstalled(Tool):
    """Found on real hardware: "Is yay installed?" had no tool, and was planned as "what's using memory"."""

    name = "packages.check"
    description = (
        "Whether software is installed, and its version: 'is yay installed?', 'do I have obs?' "
        "(read-only; installs nothing)."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.SYSTEM_READ})
    Args = CheckArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        words = cast(CheckArgs, args).name
        if missing := ungrounded_words(words, ctx.user_words or ctx.request_text):
            return ToolOutcome(
                StepStatus.FAILED, f"What to look for must be your words ({', '.join(missing)} isn't)"
            )
        return Prepared(f"See whether {words} is installed", words)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        words = cast(str, prepared.payload)
        packages = Packages(ctx.system.runner)
        pacman = bool(ctx.which("pacman"))
        for name in candidates(words) if pacman else []:
            if found := await packages.installed(name):
                package, version = found
                said = f"{name} {version}" if package == name else f"{name} — {package} {version}"
                return ToolOutcome(
                    StepStatus.DONE,
                    f"Yes, {said} is installed",
                    (Check("pacman says it's installed", True, {"package": package}),),
                )
        resolution = ctx.resolver.resolve(words)
        if resolution.status == "found" and resolution.entry is not None:  # "obs" is OBS Studio
            entry = resolution.entry
            owner = await packages.owner(entry.path) if pacman else None
            # Found on the laptop: OBS was called "not from pacman" — its package is obs-studio
            came = (
                f" ({owner[0]} {owner[1]})"
                if owner
                else " (an app, not from pacman: a Flatpak, an AppImage…)"
            )
            return ToolOutcome(
                StepStatus.DONE,
                f"Yes, {entry.name} is installed{came}",
                (Check("the app is installed", True, {"app": entry.desktop_id}),),
            )
        available = None
        for name in candidates(words) if pacman else []:
            if available := await packages.official(name):
                break
        offer = f' — {_describe(available)} can be installed: say "install {words}"' if available else ""
        return ToolOutcome(
            StepStatus.DONE,
            f"No, {words} isn't installed{offer}",
            (Check("pacman and the apps don't list it", True, {"package": words}),),
        )


# What the system needs to start, log in, show this desktop or ask for the password — and SONU
# itself. Never removed by voice; the terminal is the place for that.
_PROTECTED = frozenset(
    {
        "base", "base-devel", "glibc", "systemd", "systemd-libs", "pacman", "sudo", "polkit", "bash",
        "coreutils", "filesystem", "linux-firmware", "mkinitcpio", "dracut", "grub", "limine",
        "efibootmgr", "networkmanager", "pipewire", "wireplumber", "mesa", "hyprland", "sddm",
        "quickshell", "xdg-desktop-portal-hyprland", "python", "ollama", "ollama-cuda", "kitty",
    }
)  # fmt: skip
_PROTECTED_PREFIXES = ("linux", "nvidia", "cachyos-", "lib32-nvidia")
_SHOWN = 6  # dependencies named in the question


def _protected(name: str) -> bool:
    return name in _PROTECTED or name.startswith(_PROTECTED_PREFIXES)


class RemoveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80, description="what to remove, in the user's words")


class Remove(Tool):
    name = "packages.remove"
    description = (
        "Uninstall software by name, with the dependencies only it needed. Shows exactly what goes; the "
        "system asks for the password itself, in a window the user can watch."
    )
    risk = Risk.HIGH
    effects = frozenset({Effect.UNINSTALL})
    Args = RemoveArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        words = cast(RemoveArgs, args).name
        if missing := ungrounded_words(words, ctx.user_words or ctx.request_text):
            return ToolOutcome(
                StepStatus.FAILED, f"What to remove must be your words ({', '.join(missing)} isn't)"
            )
        if not ctx.which("pacman") or not ctx.which("pkexec"):
            return ToolOutcome(StepStatus.FAILED, "Removing software needs pacman and pkexec (polkit)")
        packages = Packages(ctx.system.runner)
        found: tuple[str, str] | None = None
        # The whole name only ("spotify player" never falls back to "spotify"): removing is destructive.
        for name in [choice] if choice else candidates(words)[:2]:
            if valid_name(name) and (found := await packages.installed(name)):
                break
        if found is None and choice:
            return ToolOutcome(StepStatus.FAILED, f"{choice} isn't installed anymore")
        if found is None:
            matches = rank(await packages.search_installed(words), words)[:5]
            if not matches:
                return ToolOutcome(StepStatus.FAILED, f"Nothing installed is called '{words}'")
            # Found on the laptop: "remove visual studio code" asked "1 installed packages could be …".
            # One match goes to the yes/no question, which names the real package.
            if len(matches) == 1:
                found = (matches[0].name, matches[0].version)
        if found is None:
            question = f"Which installed package for '{words}'?"
            return ToolOutcome(
                StepStatus.NEEDS_INPUT,
                f"{len(matches)} installed packages could be '{words}'",
                question=question,
                choices=Choices(
                    question, tuple(Choice(f"{p.name} — {p.description[:70]}", p.name) for p in matches)
                ),
            )
        name, version = found
        if _protected(name):
            return ToolOutcome(
                StepStatus.FAILED,
                f"SONU won't remove {name}: your system or SONU needs it "
                "(use a terminal if you really mean to)",
            )
        removal = await packages.removal(name)
        if removal.needed_by:
            shown = ", ".join(removal.needed_by[:_SHOWN]) + (" …" if len(removal.needed_by) > _SHOWN else "")
            return ToolOutcome(
                StepStatus.FAILED, f"{name} can't be removed: other software needs it ({shown})"
            )
        if not removal.targets:
            return ToolOutcome(StepStatus.FAILED, f"pacman couldn't work out what removing {name} takes")
        extra = [n for n, _ in removal.targets if n != name]
        also = (
            f" and {len(extra)} package{'s' if len(extra) != 1 else ''} only it needed "
            f"({', '.join(extra[:_SHOWN])}{' …' if len(extra) > _SHOWN else ''})"
            if extra
            else ""
        )
        said = f" (for '{words}')" if name not in candidates(words) else ""
        return Prepared(
            f"Remove {name} {version}{said}{also}. Your password is asked by the system itself.",
            name,
            spoken=f"remove {name}" + (f" and {len(extra)} packages only it needed" if extra else ""),
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        name = cast(str, prepared.payload)
        if not valid_name(name) or _protected(name):
            return ToolOutcome(StepStatus.FAILED, f"'{name}' can't be removed by SONU")
        packages = Packages(ctx.system.runner)

        async def gone() -> bool:
            return await packages.installed(name) is None

        problem = await _in_window(
            ctx,
            ("pkexec", "pacman", "-Rs", "--noconfirm", "--", name),
            f"Remove {name}",
            finished=gone,
            timeout_s=OFFICIAL_TIMEOUT_S,
            verb="remove",
        )
        still = await packages.installed(name)
        return outcome_from_checks(
            [Check("pacman no longer lists it", still is None, {"package": name})],
            f"Removed {name}",
            f"{name} wasn't removed: {problem or 'pacman still lists it'} (the window shows what happened)",
        )


PACKAGE_TOOLS: tuple[Tool, ...] = (Install(), Remove(), CheckInstalled())
