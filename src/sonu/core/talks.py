"""What the user and SONU said to each other (D86): "what did we talk about yesterday?", "what did you tell me
about kestrels?".

The conversation (``conversation.py``) fades after ten quiet minutes, which is right for "it" and "that" but
not for a person, who remembers this morning. Each finished request — the user's words and what SONU said back
(including a private result it read out, the way the user heard it) — is one row here:

* Local only: a SQLite file in SONU's data directory (0600), rows older than ``keep_days`` deleted.
  ``[conversations] keep = false`` keeps nothing; "forget our conversations" deletes them (it asks first).
* A dry run, a request that only looked back at the conversations, and one that deleted them aren't kept.
* What's read back is a private result: shown and said to the user, never sent to a model or the audit log.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from sonu.core.report import TaskReport
from sonu.tools.base import StepStatus

PRUNE_EVERY_S = 3600.0
# Requests that only looked back at the conversations, or deleted them, aren't kept.
NOT_KEPT = ("conversations.",)


@dataclass(frozen=True, slots=True)
class Exchange:
    at: float  # time.time()
    request: str  # the user's own words
    reply: str  # what SONU said back


class Talks:
    def __init__(self, path: Path | None, *, keep_days: int = 30) -> None:
        self.path, self.keep_days = path, keep_days
        self._db: sqlite3.Connection | None = None
        self._pruned = 0.0

    def _connect(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        if self.path is None:
            self._db = sqlite3.connect(":memory:")
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
            self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS talk (at REAL NOT NULL, request TEXT NOT NULL, reply TEXT NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS talk_at ON talk (at)")
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def add(self, request: str, reply: str, at: float | None = None) -> None:
        at = time.time() if at is None else at
        db = self._connect()
        db.execute(
            "INSERT INTO talk (at, request, reply) VALUES (?, ?, ?)", (at, request.strip(), reply.strip())
        )
        db.commit()
        if at - self._pruned > PRUNE_EVERY_S:
            self.prune(at)

    def between(self, start: float, end: float, words: tuple[str, ...] = ()) -> list[Exchange]:
        """The exchanges in that time, oldest first; ``words``: only those that say every one of them."""
        query = "SELECT at, request, reply FROM talk WHERE at >= ? AND at < ?"
        values: list[object] = [start, end]
        for word in words:
            query += " AND (request || ' ' || reply) LIKE ? ESCAPE '\\'"
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.append(f"%{escaped}%")
        rows = self._connect().execute(query + " ORDER BY at", values).fetchall()
        return [Exchange(float(at), str(request), str(reply)) for at, request, reply in rows]

    def count(self, since: float = 0.0) -> int:
        return int(self._connect().execute("SELECT COUNT(*) FROM talk WHERE at >= ?", (since,)).fetchone()[0])

    def forget(self, since: float = 0.0) -> int:
        """Delete everything said since then; returns how many exchanges went."""
        db = self._connect()
        count = db.execute("DELETE FROM talk WHERE at >= ?", (since,)).rowcount
        db.commit()
        return count

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        db = self._connect()
        db.execute("DELETE FROM talk WHERE at < ?", (now - self.keep_days * 86400,))
        db.commit()
        self._pruned = now


def told(report: TaskReport) -> str:
    """What SONU told the user: its reply and a private result (a research answer, a document's summary — the
    reply never holds one); with no reply, the result lines they saw."""
    if report.plan is None:
        return report.error or ""
    if report.plan.kind != "steps":
        return report.reply or report.plan.message
    said = [report.reply] if report.reply else []
    for record in report.steps:
        summary, outcome = record.outcome.summary.strip(), record.outcome
        if not summary or outcome.status is StepStatus.SKIPPED or (said and summary in said[0]):
            continue
        if outcome.private or not report.reply:
            said.append(summary)
    return " ".join(said) or report.headline()


def kept(report: TaskReport) -> bool:
    """Whether this request goes in the conversations (not a dry run, not one about them)."""
    if report.dry_run or report.plan is None:
        return False
    tools = [record.tool for record in report.steps]
    return not (tools and all(tool.startswith(NOT_KEPT) for tool in tools))
