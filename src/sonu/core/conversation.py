"""The conversation so far: the last few requests and what came of them.

It lets "switch **it** to workspace 3" or "play it again" mean what the user just did. It holds only
what the user saw anyway — their words, SONU's reply and the result lines — and never a private
result (window titles, the clipboard, what's playing): those are stored as "(private result)".
It fades after a few idle minutes, and lives in SONU's state directory (0700), so the daemon and the
CLI share it and a restart doesn't forget mid-task.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sonu.core.report import ICONS, TaskReport

MAX_TURNS = 6
PRIVATE = "(private result)"
TRIED_ANOTHER_WAY = "↻ Tried another way:"


@dataclass(frozen=True, slots=True)
class Turn:
    request: str  # the user's own words
    reply: str  # what SONU said back
    results: tuple[str, ...]  # "✔ Opened kitty on workspace 2" — never a private result
    at: float  # time.time()
    windows: tuple[str, ...] = ()  # addresses of the windows it acted on: what "it" means next


def results_of(report: TaskReport) -> tuple[str, ...]:
    """The report's result lines, with private ones replaced (they never reach the model)."""
    if report.plan is None or report.plan.kind != "steps":
        return ()
    lines: list[str] = []
    for index, record in enumerate(report.steps):
        if index == report.recovered_from:
            lines.append(TRIED_ANOTHER_WAY)  # D92: what follows is a second try after the failure above
        lines.append(
            f"{ICONS[record.outcome.status]} {PRIVATE if record.outcome.private else record.outcome.summary}"
        )
    return tuple(lines) + tuple(report.not_done)


@dataclass
class Conversation:
    path: Path | None = None  # None: in memory only (tests)
    idle_s: float = 600.0
    clock: Callable[[], float] = time.time
    turns: list[Turn] = field(default_factory=list)

    @classmethod
    def load(
        cls, path: Path, *, idle_s: float = 600.0, clock: Callable[[], float] = time.time
    ) -> Conversation:
        conversation = cls(path, idle_s, clock)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            conversation.turns = [
                Turn(
                    str(t["request"]),
                    str(t["reply"]),
                    tuple(map(str, t["results"])),
                    float(t["at"]),
                    tuple(map(str, t.get("windows", ()))),
                )
                for t in raw
            ]
        except (OSError, ValueError, KeyError, TypeError):
            conversation.turns = []
        return conversation

    def recent(self) -> list[Turn]:
        """The turns still in the conversation: it ends after ``idle_s`` without a request."""
        if self.turns and self.clock() - self.turns[-1].at > self.idle_s:
            self.turns = []
        return self.turns[-MAX_TURNS:]

    def user_words(self) -> str:
        """The user's own recent words (a search can use them: "play it again")."""
        return " ".join(turn.request for turn in self.recent())

    def add(self, request: str, reply: str, results: tuple[str, ...], windows: tuple[str, ...] = ()) -> None:
        self.recent()
        self.turns = [*self.turns, Turn(request, reply, results, self.clock(), windows)][-MAX_TURNS:]
        self._save()

    def last_windows(self) -> tuple[str, ...]:
        """The windows the latest request that touched any acted on ("move it to 3")."""
        return next((turn.windows for turn in reversed(self.recent()) if turn.windows), ())

    def clear(self) -> None:
        self.turns = []
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(turn) for turn in self.turns], ensure_ascii=False), encoding="utf-8"
        )
        tmp.chmod(0o600)
        tmp.replace(self.path)
