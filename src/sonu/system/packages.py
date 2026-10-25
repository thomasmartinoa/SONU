"""Arch packages: what's installed, what's in the official repos, what's in the AUR (M4.5, D54).

Read-only here: ``pacman -Q`` / ``-Si`` / ``-Ss`` and ``yay -Sia`` / ``-Ssa`` (AUR lookups go to
aur.archlinux.org with the user's words — like a web search). Installing happens in a terminal SONU
opens (``sonu.tools.package_tools``), with the system's own password prompt (pkexec/polkit).
Formats read from pacman 7 and yay 13 on the owner's laptop (2026-09-28), with ``LC_ALL=C``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sonu.system.commands import CommandError, CommandRunner, ProgramMissing

# A package name as pacman accepts it — never starting with "-", which pacman would read as an option.
_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,99}$")
_SEARCH_LINE = re.compile(r"^(\S+)/(\S+) (\S+)(.*)$")
_VOTES = re.compile(r"\(\+(\d+) ")


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    repo: str  # core, extra, multilib … or "aur"
    version: str
    description: str = ""
    size: str = ""  # download size (official) — "" for the AUR
    votes: int | None = None  # AUR only

    @property
    def aur(self) -> bool:
        return self.repo == "aur"


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name))


def candidates(words: str) -> list[str]:
    """Package names the user's words may mean: "obs studio" → obs-studio, obsstudio, obs."""
    base = re.sub(r"[^a-z0-9@._+ -]", "", words.lower()).strip()
    parts = [part.strip("-") for part in base.split() if part.strip("-")]
    names = ["-".join(parts), "".join(parts), *parts[:1]] if parts else []
    return [n for n in dict.fromkeys(names) if valid_name(n)]


def parse_info(text: str) -> Package | None:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and key[0] != " ":
            fields.setdefault(key.strip(), value.strip())
    if "Name" not in fields:
        return None
    votes = fields.get("Votes", "")
    return Package(
        fields["Name"],
        fields.get("Repository", ""),
        fields.get("Version", ""),
        fields.get("Description", ""),
        fields.get("Download Size", ""),
        int(votes) if votes.isdigit() else None,
    )


def parse_search(text: str) -> list[Package]:
    found: list[Package] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if match := _SEARCH_LINE.match(line):
            description = lines[index + 1].strip() if index + 1 < len(lines) else ""
            votes = _VOTES.search(match.group(4))
            found.append(
                Package(
                    match.group(2),
                    match.group(1),
                    match.group(3),
                    description,
                    votes=int(votes.group(1)) if votes else None,
                )
            )
    return found


class Packages:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def _run(self, *argv: str, timeout_s: float = 20.0) -> tuple[bool, str]:
        try:
            result = await self.runner.run(argv, timeout_s=timeout_s)
        except ProgramMissing:
            return False, ""
        except CommandError:
            return False, ""
        return result.ok, result.stdout

    async def installed(self, name: str) -> tuple[str, str] | None:
        """(the installed package, its version), or None. Found on the laptop: ``pacman -Q code``
        answers "visual-studio-code-bin 1.139.1-1" — the package that provides it."""
        ok, out = await self._run("pacman", "-Q", name)
        parts = out.split()
        return (parts[0], parts[1]) if ok and len(parts) >= 2 else None

    async def owner(self, path: Path) -> tuple[str, str] | None:
        """(the package, its version) a file came with (``pacman -Qo``), or None: not from pacman."""
        ok, out = await self._run("pacman", "-Qo", "--", str(path))
        said = re.search(r" is owned by (\S+) (\S+)$", out.strip())
        return (said.group(1), said.group(2)) if ok and said else None

    async def official(self, name: str) -> Package | None:
        ok, out = await self._run("pacman", "-Si", name)
        return parse_info(out) if ok else None

    async def aur(self, name: str) -> Package | None:
        ok, out = await self._run("yay", "-Sia", name, timeout_s=30.0)
        info = parse_info(out) if ok else None
        return info if info is not None and info.name == name else None

    async def search_official(self, words: str) -> list[Package]:
        terms = [t for t in re.split(r"\s+", words.strip()) if t]
        if not terms:
            return []
        ok, out = await self._run("pacman", "-Ss", *terms)
        return parse_search(out) if ok else []

    async def search_installed(self, words: str) -> list[Package]:
        """Installed packages whose name or description has all the words (``pacman -Qs``)."""
        terms = [t for t in re.split(r"\s+", words.strip()) if t and not t.startswith("-")]
        if not terms:
            return []
        ok, out = await self._run("pacman", "-Qs", "--", *terms)
        return parse_search(out) if ok else []

    async def removal(self, name: str) -> Removal:
        """What removing ``name`` with its no-longer-needed dependencies would take (a preview)."""
        argv = ("pacman", "-Rsp", "--print-format", "%n %v", "--", name)
        try:
            result = await self.runner.run(argv, timeout_s=20.0)
        except (ProgramMissing, CommandError):
            return Removal()
        if not result.ok:
            return Removal(needed_by=tuple(dict.fromkeys(_NEEDED_BY.findall(result.stderr + result.stdout))))
        targets = tuple(
            (parts[0], parts[1]) for line in result.stdout.splitlines() if len(parts := line.split()) == 2
        )
        return Removal(targets=targets)

    async def search_aur(self, words: str) -> list[Package]:
        terms = [t for t in re.split(r"\s+", words.strip()) if t]
        if not terms:
            return []
        ok, out = await self._run("yay", "-Ssa", "--sortby", "votes", *terms, timeout_s=30.0)
        return parse_search(out) if ok else []


@dataclass(frozen=True, slots=True)
class Removal:
    """What ``pacman -Rs`` would take away (``pacman -Rsp``: no root, nothing changes)."""

    targets: tuple[tuple[str, str], ...] = ()  # (name, version): the package, then what only it needed
    needed_by: tuple[str, ...] = ()  # installed packages that need it: pacman refuses


_NEEDED_BY = re.compile(r"breaks dependency '[^']+' required by (\S+)")


def rank(packages: list[Package], words: str) -> list[Package]:
    """Best first: the name the user said, then names starting with it, then names with all their
    words, then descriptions; the AUR by votes. Found on the laptop: pacman lists alphabetically, so
    "spotify" put gst-plugin-spotify before spotify-launcher. Found on real hardware: a misheard "Cozy"
    offered dunesh ("The cozy shell") — a description alone doesn't make a package for a single word."""
    names = candidates(words)
    terms = [t for t in re.split(r"[^a-z0-9]+", words.lower()) if t]

    def score(package: Package) -> tuple[int, int]:
        name = package.name.lower()
        if name in names:
            points = 100
        elif any(name.startswith(n + "-") or name == n + "d" for n in names[:1]):
            points = 60
        elif terms and all(t in name for t in terms):
            points = 40
        elif len(terms) > 1 and all(t in package.description.lower() for t in terms):
            points = (
                10  # "music player" may be described; one word is a name — "cozy" is not "The cozy shell"
            )
        else:
            points = 0
        return points, package.votes or 0

    scored = [(score(p), p) for p in packages]
    return [p for s, p in sorted(scored, key=lambda item: item[0], reverse=True) if s[0] > 0]
