"""`sonu memory` and `sonu conversations` (D90, part 3): seen and deleted by the user; deleting asks first and
is audited — how many, never what."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.audit.log import AuditLog
from sonu.cli.app import app
from sonu.config.paths import SonuPaths
from sonu.core.memory import Memory, MemoryItem
from sonu.core.talks import Talks


@pytest.fixture
def env(xdg_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    (tmp_path / "run").mkdir()
    return xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}  # never the real sonud


def records(env: dict[str, str]) -> list[dict[str, object]]:
    return [r.data for r in AuditLog(SonuPaths.from_env(env).audit_file).records() if r.kind == "data.forget"]


def test_memory_is_listed_and_forgotten(env: dict[str, str]) -> None:
    paths = SonuPaths.from_env(env)
    memory = Memory.load(paths.data_dir / "memory.json")
    memory.add(MemoryItem("note", "the wifi password is on the fridge"))
    memory.add(MemoryItem("name", "Martin"))
    runner = CliRunner()
    listed = json.loads(runner.invoke(app, ["memory", "list", "--json"], env=env).stdout)
    assert [(row["n"], row["text"]) for row in listed] == [
        (1, "the wifi password is on the fridge"),
        (2, "your name is Martin"),
    ]
    refused = runner.invoke(app, ["memory", "forget", "1"], env=env)
    assert refused.exit_code == 1 and "Nothing deleted" in refused.output
    done = runner.invoke(app, ["memory", "forget", "1", "--yes"], env=env)
    assert done.exit_code == 0, done.output
    assert [item.text for item in Memory.load(paths.data_dir / "memory.json").items] == ["Martin"]
    assert records(env) == [{"what": "memory", "count": 1}]  # never the note itself
    assert runner.invoke(app, ["memory", "forget", "9", "--yes"], env=env).exit_code == 1


def test_conversations_are_listed_and_deleted(env: dict[str, str]) -> None:
    paths = SonuPaths.from_env(env)
    talks = Talks(paths.data_dir / "conversations.sqlite3")
    talks.add("open kitty on 2", "Kitty is up on workspace 2.", time.time() - 60)
    talks.close()
    runner = CliRunner()
    listed = json.loads(runner.invoke(app, ["conversations", "list", "today", "--json"], env=env).stdout)
    assert [(row["request"], row["reply"]) for row in listed] == [
        ("open kitty on 2", "Kitty is up on workspace 2.")
    ]
    done = runner.invoke(app, ["conversations", "forget", "--since", "everything", "--yes"], env=env)
    assert done.exit_code == 0 and "Deleted 1 request" in done.output
    assert json.loads(runner.invoke(app, ["conversations", "list", "all", "--json"], env=env).stdout) == []
    assert records(env) == [{"what": "conversations", "count": 1}]


def test_the_journal_for_the_window(env: dict[str, str]) -> None:
    shown = json.loads(CliRunner().invoke(app, ["journal", "show", "today", "--json"], env=env).stdout)
    assert shown == {"summary": "Nothing in the journal today", "paused": False}
