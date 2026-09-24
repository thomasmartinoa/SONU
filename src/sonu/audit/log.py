"""Append-only, hash-chained audit log (JSON Lines).

Every record carries the SHA-256 of the previous record, so editing, reordering or deleting
a line in the middle breaks the chain and ``verify()`` reports where.

Honest limits: this makes tampering *evident*, it does not make it *impossible*. Anyone who
can write the file can also rewrite the whole chain. It protects against silent corruption
and casual edits, and gives users and reviewers a trustworthy-by-default history.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from sonu.config.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE
from sonu.redact import redact_value

GENESIS_HASH = "0" * 64
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TAIL_CHUNK = 64 * 1024


class AuditError(Exception):
    """The audit log cannot be read or appended to safely."""


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    ts: str
    kind: str
    data: dict[str, Any]
    prev: str
    hash: str


@dataclass(frozen=True, slots=True)
class AuditVerification:
    ok: bool
    records: int
    error: str | None = None
    line: int | None = None


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_hash(*, seq: int, ts: str, kind: str, data: Mapping[str, Any], prev: str) -> str:
    body = {"seq": seq, "ts": ts, "kind": kind, "data": data, "prev": prev}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _to_json_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secrets and normalise to plain JSON types (paths, enums... become strings)."""
    cleaned = redact_value(dict(data))
    normalised: dict[str, Any] = json.loads(json.dumps(cleaned, default=str))
    return normalised


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------------------------------------------------------ writing
    def append(self, kind: str, data: Mapping[str, Any] | None = None) -> AuditRecord:
        if not _KIND_RE.match(kind):
            raise ValueError(f"invalid audit kind {kind!r}")
        payload = _to_json_data(data or {})

        self.path.parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, PRIVATE_FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            last = self._read_last_record(fd)
            seq = 0 if last is None else last.seq + 1
            prev = GENESIS_HASH if last is None else last.hash
            ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            record = AuditRecord(
                seq=seq,
                ts=ts,
                kind=kind,
                data=payload,
                prev=prev,
                hash=compute_hash(seq=seq, ts=ts, kind=kind, data=payload, prev=prev),
            )
            line = record.model_dump_json().encode("utf-8") + b"\n"
            written = os.write(fd, line)
            if written != len(line):
                raise AuditError(f"short write to {self.path} ({written}/{len(line)} bytes)")
            os.fsync(fd)
            return record
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_last_record(self, fd: int) -> AuditRecord | None:
        size = os.lseek(fd, 0, os.SEEK_END)
        if size == 0:
            return None
        # Read backwards until we hold the complete last line.
        buffer = b""
        position = size
        while position > 0:
            step = min(_TAIL_CHUNK, position)
            position -= step
            buffer = os.pread(fd, step, position) + buffer
            if buffer.count(b"\n") >= 2 or position == 0:
                break
        if not buffer.endswith(b"\n"):
            raise AuditError(
                f"{self.path}: last record is incomplete (interrupted write?). "
                "Run `sonu audit verify` and inspect the file before continuing."
            )
        last_line = buffer.rstrip(b"\n").rsplit(b"\n", 1)[-1]
        try:
            return AuditRecord.model_validate_json(last_line)
        except ValidationError as exc:
            raise AuditError(f"{self.path}: last record is not valid: {exc.errors()[0]['msg']}") from exc

    # ------------------------------------------------------------------ reading
    def _lines(self) -> Iterator[tuple[int, bytes]]:
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            yield from enumerate(handle, start=1)

    def records(self) -> Iterator[AuditRecord]:
        for number, raw in self._lines():
            try:
                yield AuditRecord.model_validate_json(raw)
            except ValidationError as exc:
                raise AuditError(f"{self.path}:{number}: not a valid audit record") from exc

    def tail(self, count: int) -> list[AuditRecord]:
        if count <= 0:
            return []
        return list(deque(self.records(), maxlen=count))

    def verify(self) -> AuditVerification:
        expected_prev = GENESIS_HASH
        count = 0  # records verified so far == the seq the next record must carry
        for number, raw in self._lines():
            if not raw.endswith(b"\n"):
                return AuditVerification(False, count, "incomplete last line (interrupted write?)", number)
            try:
                record = AuditRecord.model_validate_json(raw)
            except ValidationError:
                return AuditVerification(False, count, "line is not a valid audit record", number)
            if record.seq != count:
                return AuditVerification(
                    False, count, f"sequence gap: expected {count}, found {record.seq}", number
                )
            if record.prev != expected_prev:
                return AuditVerification(False, count, "chain broken: 'prev' does not match", number)
            recomputed = compute_hash(
                seq=record.seq, ts=record.ts, kind=record.kind, data=record.data, prev=record.prev
            )
            if recomputed != record.hash:
                return AuditVerification(False, count, "record content does not match its hash", number)
            expected_prev = record.hash
            count += 1
        return AuditVerification(True, count)
