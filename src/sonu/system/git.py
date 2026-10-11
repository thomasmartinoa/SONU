"""Read-only git: "what did I change in SONU?", "what are the last commits?" (D65).

Only reading commands, with the repository's own hooks kept from running: no fsmonitor program, no
external diff or textconv drivers, no optional locks (``git status`` would otherwise write the index).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sonu.system.commands import CommandError, CommandRunner, ProgramMissing

GIT_TIMEOUT_S = 10.0
MAX_COMMITS = 5


def _git(folder: Path, *args: str) -> tuple[str, ...]:
    return ("git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(folder), *args)


@dataclass(frozen=True, slots=True)
class Changed:
    path: str
    kind: str  # "changed", "new", "deleted", "renamed", "untracked"
    added: int | None = None
    removed: int | None = None


@dataclass(frozen=True, slots=True)
class Changes:
    branch: str  # "" when detached
    upstream: str
    ahead: int
    behind: int
    files: tuple[Changed, ...] = field(default_factory=tuple)

    @property
    def untracked(self) -> tuple[Changed, ...]:
        return tuple(f for f in self.files if f.kind == "untracked")

    @property
    def edited(self) -> tuple[Changed, ...]:
        return tuple(f for f in self.files if f.kind != "untracked")


@dataclass(frozen=True, slots=True)
class Commit:
    short: str
    when: str  # "2 hours ago"
    subject: str


class NotARepo(CommandError):
    pass


async def _run(runner: CommandRunner, folder: Path, *args: str) -> str:
    try:
        result = await runner.run(_git(folder, *args), timeout_s=GIT_TIMEOUT_S)
    except ProgramMissing as exc:
        raise CommandError("git isn't installed (install: sudo pacman -S git)") from exc
    if not result.ok:
        if "not a git repository" in result.stderr.lower():
            raise NotARepo(f"{folder.name} isn't a git repository")
        raise CommandError(result.problem())
    return result.stdout


def _branch_line(line: str) -> tuple[str, str, int, int]:
    """``## main...origin/main [ahead 2, behind 1]`` → (main, origin/main, 2, 1)."""
    text = line.removeprefix("## ")
    counts = ""
    if text.endswith("]") and " [" in text:
        text, _, counts = text.partition(" [")
    branch, _, upstream = text.partition("...")
    if branch.startswith("No commits yet on "):
        branch = branch.removeprefix("No commits yet on ")
    if branch.startswith("HEAD (no branch)"):
        branch = ""
    ahead = behind = 0
    for part in counts.rstrip("]").split(", "):
        word, _, number = part.partition(" ")
        if number.isdigit():
            ahead, behind = (int(number), behind) if word == "ahead" else (ahead, int(number))
    return branch, upstream, ahead, behind


_KINDS = {"A": "new", "D": "deleted", "R": "renamed", "C": "new"}


async def changes(runner: CommandRunner, folder: Path) -> Changes:
    status = await _run(runner, folder, "status", "--porcelain=v1", "--branch", "-z")
    entries = status.split("\0")
    branch, upstream, ahead, behind = _branch_line(entries[0]) if entries else ("", "", 0, 0)
    files: list[Changed] = []
    rest = iter(entries[1:])
    for entry in rest:
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            next(rest, None)  # the old name
        if code == "??":
            files.append(Changed(path, "untracked"))
            continue
        letter = next((c for c in code if c in _KINDS), "")
        files.append(Changed(path, _KINDS.get(letter, "changed")))
    numbers: dict[str, tuple[int | None, int | None]] = {}
    try:
        stat = await _run(runner, folder, "diff", "HEAD", "--numstat", "--no-ext-diff", "--no-textconv")
    except NotARepo:
        raise
    except CommandError:
        stat = ""  # no commits yet
    for line in stat.splitlines():
        added, removed, path = [*line.split("\t", 2), "", "", ""][:3]
        if path:
            numbers[path.split(" => ")[-1].rstrip("}")] = (
                int(added) if added.isdigit() else None,
                int(removed) if removed.isdigit() else None,
            )
    counted = tuple(
        Changed(f.path, f.kind, *numbers.get(f.path, (None, None))) if f.kind != "untracked" else f
        for f in files
    )
    return Changes(branch, upstream, ahead, behind, counted)


async def recent_commits(runner: CommandRunner, folder: Path, count: int = MAX_COMMITS) -> list[Commit]:
    try:
        out = await _run(
            runner, folder, "log", f"-n{count}", "--no-show-signature", "--format=%h%x1f%cr%x1f%s%x1e"
        )
    except NotARepo:
        raise
    except CommandError:
        return []  # no commits yet
    commits = []
    for record in out.split("\x1e"):
        parts = record.strip().split("\x1f")
        if len(parts) == 3:
            commits.append(Commit(*parts))
    return commits
