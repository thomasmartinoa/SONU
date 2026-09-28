""" "What are my pull requests?" and "merge the pull request" — GitHub via ``gh`` (M4 part 3, D53).

SONU finds the pull request itself (the repo the user named; the only one open; else it asks which,
from choices it built). It won't offer a merge that GitHub would refuse or that isn't ready: a draft,
conflicts, failing or still-running checks. Merging is high risk — by voice it needs the OK on screen
too (D42) — and it's done only when GitHub says the pull request is merged.
"""

from __future__ import annotations

import re
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.system.commands import CommandError
from sonu.system.github import GitHub, PullRequest
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


def _github(ctx: ToolContext) -> GitHub:
    return GitHub(ctx.system.runner)


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}


def _in_repo(prs: list[PullRequest], repo: str | None) -> list[PullRequest]:
    """The pull requests in the repo the user's words name ("SONU", "hypr dotfiles")."""
    if not repo:
        return prs
    wanted = "".join(sorted(_words(repo) - {"repo", "the", "my", "in", "on", "github"}))
    return [pr for pr in prs if "".join(sorted(_words(pr.repo.split("/")[-1]))) == wanted] or [
        pr for pr in prs if _words(repo) & _words(pr.repo.split("/")[-1])
    ]


# ---------------------------------------------------------------------------- github.prs
class PrsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str | None = Field(default=None, max_length=80, description="only this repo (user's words)")


class PullRequests(Tool):
    name = "github.prs"
    description = "The user's open pull requests on GitHub, with their checks."
    risk = Risk.LOW
    effects = frozenset({Effect.SYSTEM_READ})
    Args = PrsArgs
    grounded = ("repo",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        repo = cast(PrsArgs, args).repo
        return Prepared(f"List your open pull requests{f' in {repo}' if repo else ''}", repo)

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        repo = cast("str | None", prepared.payload)
        github = _github(ctx)
        try:
            prs = _in_repo(await github.open_prs(), repo)
            detailed = [await github.pull_request(pr.repo, pr.number) for pr in prs[:8]]
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't ask GitHub: {exc}")
        check = Check("asked GitHub", True, {"open": len(prs)})
        where = f" in {repo}" if repo else ""
        if not detailed:
            return ToolOutcome(StepStatus.DONE, f"No open pull requests{where}", (check,))
        parts = [
            f"{pr.name} {pr.title_shown} — {'draft' if pr.draft else f'checks {pr.checks}'}"
            for pr in detailed
        ]
        count = f"{len(prs)} open pull request{'s' if len(prs) != 1 else ''}{where}"
        return ToolOutcome(StepStatus.DONE, f"{count}: " + " · ".join(parts), (check,))


# ---------------------------------------------------------------------------- github.merge
class MergeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str | None = Field(default=None, max_length=80, description="the repo (user's words)")
    number: int | None = Field(
        default=None, ge=1, description="the pull request number, only if the user said it"
    )


class Merge(Tool):
    name = "github.merge"
    description = "Merge one of the user's open pull requests on GitHub (after they approve it on screen)."
    risk = Risk.HIGH
    effects = frozenset({Effect.GITHUB_WRITE})
    Args = MergeArgs
    grounded = ("repo",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(MergeArgs, args)
        if a.repo and (missing := ungrounded_words(a.repo, ctx.user_words or ctx.request_text)):
            return ToolOutcome(StepStatus.FAILED, f"The repo must be your words ({', '.join(missing)} isn't)")
        if a.number is not None and str(a.number) not in re.findall(
            r"\d+", ctx.user_words or ctx.request_text
        ):
            a = a.model_copy(update={"number": None})  # a number the user didn't say: SONU finds it
        github = _github(ctx)
        try:
            prs = _in_repo(await github.open_prs(), a.repo)
            if choice:
                prs = [pr for pr in prs if f"{pr.repo}#{pr.number}" == choice]
            elif a.number is not None:
                prs = [pr for pr in prs if pr.number == a.number]
            if len(prs) > 1:
                options = tuple(
                    Choice(f"{pr.name} {pr.title_shown}", f"{pr.repo}#{pr.number}") for pr in prs[:8]
                )
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT,
                    f"{len(prs)} open pull requests",
                    question="Which pull request should I merge?",
                    choices=Choices("Which pull request should I merge?", options),
                )
            if not prs:
                where = f" in {a.repo}" if a.repo else ""
                number = f" #{a.number}" if a.number else ""
                return ToolOutcome(StepStatus.FAILED, f"There's no open pull request{number}{where}")
            pr = await github.pull_request(prs[0].repo, prs[0].number)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't ask GitHub: {exc}")
        if problem := _not_ready(pr):
            return ToolOutcome(StepStatus.FAILED, f"Not merging {pr.name}: {problem}")
        size = f"{pr.changed_files} files, +{pr.additions} -{pr.deletions}"
        return Prepared(
            f"Merge pull request {pr.name} {pr.title_shown} ({size}, checks {pr.checks}) on GitHub",
            pr,
            spoken=f"merge pull request {pr.number} in {pr.repo.split('/')[-1]}",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        pr = cast(PullRequest, prepared.payload)
        github = _github(ctx)
        try:
            await github.merge(pr.repo, pr.number)
            after = await github.pull_request(pr.repo, pr.number)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"GitHub didn't merge {pr.name}: {exc}")
        return outcome_from_checks(
            [Check("GitHub says it's merged", after.state == "MERGED", {"state": after.state})],
            f"Merged {pr.name} on GitHub",
            f"Asked GitHub to merge {pr.name}, but it's {after.state.lower()}",
        )


def _not_ready(pr: PullRequest) -> str | None:
    if pr.state != "OPEN":
        return f"it's {pr.state.lower()} already"
    if pr.draft:
        return "it's a draft"
    if pr.mergeable == "CONFLICTING":
        return "it has conflicts with its base branch"
    if pr.checks == "failing":
        return "its checks are failing"
    if pr.checks == "running":
        return "its checks are still running — ask again when they're done"
    return None


GITHUB_TOOLS: tuple[Tool, ...] = (PullRequests(), Merge())
