from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from sonu.audit import AuditError, AuditLog
from sonu.audit.log import GENESIS_HASH
from sonu.redact import REDACTED


@pytest.fixture
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit" / "audit.jsonl")


def test_empty_log_verifies(log: AuditLog) -> None:
    result = log.verify()
    assert result.ok
    assert result.records == 0


def test_append_builds_a_hash_chain(log: AuditLog) -> None:
    first = log.append("task.start", {"request": "open kitty"})
    second = log.append("tool.call", {"tool": "apps.launch", "args": {"app": "kitty"}})
    assert (first.seq, second.seq) == (0, 1)
    assert first.prev == GENESIS_HASH
    assert second.prev == first.hash
    assert log.verify().ok
    assert [record.kind for record in log.records()] == ["task.start", "tool.call"]


def test_file_is_private(log: AuditLog) -> None:
    log.append("doctor.run", {})
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.path.parent.stat().st_mode) == 0o700


def test_secrets_are_redacted_before_writing(log: AuditLog) -> None:
    record = log.append("provider.call", {"api_key": "sk-live-abcdefghijklmnopqrstuvwxyz", "note": "hi"})
    assert record.data["api_key"] == REDACTED
    assert "abcdefghijklmnop" not in log.path.read_text()


def test_edited_record_is_detected(log: AuditLog) -> None:
    for index in range(3):
        log.append("step", {"n": index})
    lines = log.path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["data"]["n"] = 42
    lines[1] = json.dumps(tampered)
    log.path.write_text("\n".join(lines) + "\n")
    result = log.verify()
    assert not result.ok
    assert result.line == 2
    assert result.error is not None and "hash" in result.error


def test_deleted_record_is_detected(log: AuditLog) -> None:
    for index in range(3):
        log.append("step", {"n": index})
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    result = log.verify()
    assert not result.ok
    assert result.line == 2


def test_incomplete_last_line_blocks_appending(log: AuditLog) -> None:
    log.append("step", {})
    with log.path.open("a") as handle:
        handle.write('{"seq": 1, "ts": "half-writ')
    assert not log.verify().ok
    with pytest.raises(AuditError, match="incomplete"):
        log.append("step", {})


def test_tail_returns_the_newest_records(log: AuditLog) -> None:
    for index in range(5):
        log.append("step", {"n": index})
    assert [record.data["n"] for record in log.tail(2)] == [3, 4]
    assert log.tail(0) == []


@pytest.mark.parametrize("kind", ["", "Upper", "has space", "1starts-with-digit", "x" * 65])
def test_invalid_kinds_are_rejected(log: AuditLog, kind: str) -> None:
    with pytest.raises(ValueError, match="invalid audit kind"):
        log.append(kind, {})


def test_non_json_values_are_stringified(log: AuditLog, tmp_path: Path) -> None:
    record = log.append("step", {"path": tmp_path})
    assert record.data["path"] == str(tmp_path)
    assert log.verify().ok
