"""GitHub through the ``gh`` CLI the user is already signed in to (M4 part 3, D53).

Read: the open pull requests that involve the user, and one pull request's checks and state. Write:
merging one — only after the user approved it (high risk), and confirmed from GitHub afterwards.
Pull request titles are written on GitHub, possibly by someone else: SONU shows them, quoted; they
are data, never instructions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sonu.system.commands import CommandError, CommandRunner, ProgramMissing

_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo: str  # owner/name
    number: int
    title: str
    draft: bool = False
    state: str = "OPEN"  # OPEN, MERGED, CLOSED
    checks: str = "none"  # passing, failing, running, none
    mergeable: str = "UNKNOWN"  # MERGEABLE, CONFLICTING, UNKNOWN
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0

    @property
    def name(self) -> str:
        """How SONU says it: "SONU #31"."""
        return f"{self.repo.split('/')[-1]} #{self.number}"

    @property
    def title_shown(self) -> str:
        title = " ".join(self.title.split())
        return f"“{title[:70]}{'…' if len(title) > 70 else ''}”"


def checks_state(rollup: list[dict[str, Any]]) -> str:
    """One word for a PR's checks. Each check counts once (its latest run)."""
    latest: dict[str, dict[str, Any]] = {}
    for check in rollup:
        latest[str(check.get("name") or check.get("context") or "")] = check
    if not latest:
        return "none"
    results = [str(c.get("conclusion") or c.get("state") or "").upper() for c in latest.values()]
    statuses = [str(c.get("status") or "COMPLETED").upper() for c in latest.values()]
    if any(result in _FAILED for result in results):
        return "failing"
    if any(status != "COMPLETED" for status in statuses) or any(r in ("PENDING", "") for r in results):
        return "running"
    return "passing"


class GitHub:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def _gh(self, *args: str, timeout_s: float = 20.0) -> str:
        try:
            result = await self.runner.run(("gh", *args), timeout_s=timeout_s)
        except ProgramMissing as exc:
            raise CommandError("the GitHub CLI (gh) isn't installed") from exc
        if not result.ok:
            raise CommandError(result.problem())
        return result.stdout

    async def open_prs(self) -> list[PullRequest]:
        """Open pull requests that involve the user (author, reviewer, assignee, mentioned)."""
        out = await self._gh(
            "search", "prs", "--involves", "@me", "--state", "open", "--limit", "30",
            "--json", "repository,number,title,isDraft",
        )  # fmt: skip
        found = [
            PullRequest(
                str(item["repository"]["nameWithOwner"]),
                int(item["number"]),
                str(item.get("title", "")),
                bool(item.get("isDraft")),
            )
            for item in json.loads(out or "[]")
        ]
        return sorted(found, key=lambda pr: (pr.repo.lower(), pr.number))

    async def pull_request(self, repo: str, number: int) -> PullRequest:
        out = await self._gh(
            "pr", "view", str(number), "--repo", repo, "--json",
            "number,title,state,isDraft,mergeable,statusCheckRollup,changedFiles,additions,deletions",
        )  # fmt: skip
        data = json.loads(out)
        return PullRequest(
            repo,
            int(data["number"]),
            str(data.get("title", "")),
            bool(data.get("isDraft")),
            str(data.get("state", "OPEN")),
            checks_state(list(data.get("statusCheckRollup") or [])),
            str(data.get("mergeable", "UNKNOWN")),
            int(data.get("changedFiles") or 0),
            int(data.get("additions") or 0),
            int(data.get("deletions") or 0),
        )

    async def merge(self, repo: str, number: int) -> None:
        """A merge commit, like the owner's own merges ("Merge pull request #29 from …")."""
        await self._gh("pr", "merge", str(number), "--repo", repo, "--merge", timeout_s=60.0)
