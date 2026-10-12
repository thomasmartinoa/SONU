"""A project's command in SONU's window (D65), and the notification a long one ends with (D83)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sonu.integrations import run_task


@pytest.fixture
def shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in notify-send on the PATH that writes down what it was asked to show."""
    bin_dir, log = tmp_path / "bin", tmp_path / "shown.txt"
    bin_dir.mkdir()
    fake = bin_dir / "notify-send"
    fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {log}\necho >> {log}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log


def test_a_long_run_ends_with_a_notification(
    tmp_path: Path, shown: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_task, "NOTIFY_AFTER_S", 0.0)
    status = tmp_path / "a.exit"
    assert run_task.main([str(status), "--notify=uv run pytest in SONU", "true"]) == 0
    assert status.read_text() == "0"
    assert run_task.main([str(status), "--notify=npm run build in shop", "false"]) == 1
    passed, failed = shown.read_text().splitlines()
    assert "|✔ uv run pytest in SONU passed|after 0 s — the output is in its window|" in passed
    assert "critical" in failed and "✖ npm run build in shop failed (exit code 1)" in failed


def test_a_short_run_is_reported_by_sonu_not_notified(tmp_path: Path, shown: Path) -> None:
    status = tmp_path / "a.exit"
    assert run_task.main([str(status), "--notify=uv run pytest in SONU", "true"]) == 0
    assert not shown.exists()  # under NOTIFY_AFTER_S: SONU's own answer says it
    assert run_task.main([str(status), "true"]) == 0  # no label: never
    assert not shown.exists()
