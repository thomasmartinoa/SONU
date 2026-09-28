"""A fake ``gh`` (the GitHub CLI) for tests and evals: open pull requests, their checks, merging."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakePr:
    repo: str
    number: int
    title: str
    draft: bool = False
    state: str = "OPEN"
    checks: list[tuple[str, str, str]] = field(default_factory=lambda: [("CI", "COMPLETED", "SUCCESS")])
    mergeable: str = "MERGEABLE"


def default_prs() -> list[FakePr]:
    return [
        FakePr("thomasmartinoa/SONU", 31, "M4 part 3: GitHub"),
        FakePr(
            "thomasmartinoa/hypr-dotfiles", 5, "Night light fix", checks=[("lint", "COMPLETED", "FAILURE")]
        ),
    ]


@dataclass
class FakeGitHub:
    prs: list[FakePr] = field(default_factory=default_prs)
    merged: list[str] = field(default_factory=list)  # "owner/repo#number"

    def find(self, repo: str, number: int) -> FakePr | None:
        return next((pr for pr in self.prs if pr.repo == repo and pr.number == number), None)

    def handle(self, args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["search", "prs"]:
            found = [
                {
                    "repository": {"nameWithOwner": pr.repo},
                    "number": pr.number,
                    "title": pr.title,
                    "isDraft": pr.draft,
                }
                for pr in self.prs
                if pr.state == "OPEN"
            ]
            return 0, json.dumps(found), ""
        if args[:2] in (["pr", "view"], ["pr", "merge"]):
            number, repo = int(args[2]), args[args.index("--repo") + 1]
            pr = self.find(repo, number)
            if pr is None:
                return 1, "", f"GraphQL: Could not resolve to a PullRequest with the number of {number}."
            if args[1] == "merge":
                if pr.state != "OPEN" or pr.mergeable == "CONFLICTING":
                    return 1, "", "Pull request is not mergeable"
                pr.state = "MERGED"
                self.merged.append(f"{repo}#{number}")
                return 0, "", ""
            view: dict[str, Any] = {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "isDraft": pr.draft,
                "mergeable": pr.mergeable,
                "statusCheckRollup": [
                    {"name": name, "status": status, "conclusion": conclusion}
                    for name, status, conclusion in pr.checks
                ],
                "changedFiles": 3,
                "additions": 120,
                "deletions": 8,
            }
            return 0, json.dumps(view), ""
        return 1, "", f"unsupported gh call {args}"
