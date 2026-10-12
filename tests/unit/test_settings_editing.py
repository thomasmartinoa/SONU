"""Changing settings without editing the file (D90): checked, comments kept, asked first, audited."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sonu.audit.log import AuditLog
from sonu.cli.app import app
from sonu.config.catalog import BY_KEY, CATALOG
from sonu.config.editing import SettingsError, change, loosens, parse, value_of
from sonu.config.paths import SonuPaths
from sonu.config.settings import DEFAULT_CONFIG_TEMPLATE, Settings

MINE = """\
# my own note — keep me
[voice]
speed = 1.2  # a bit faster

[heads_up]
# enabled = false
"""


def test_a_change_keeps_the_users_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(MINE)
    settings, old = change(path, "heads_up.enabled", True)
    assert settings.heads_up.enabled and old is False
    text = path.read_text()
    assert "# my own note — keep me" in text and "speed = 1.2  # a bit faster" in text
    assert "enabled = true" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    change(path, "privacy.cloud", "allow")  # a table that wasn't in the file
    assert "[privacy]" in path.read_text()
    settings, _ = change(path, "voice.speed", None)  # back to the default
    assert settings.voice.speed == 1.0 and "speed" not in path.read_text()


def test_nothing_is_written_when_the_result_isnt_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(MINE)
    with pytest.raises(SettingsError, match=r"heads_up\.quiet"):
        change(path, "heads_up.quiet", "late at night")
    with pytest.raises(SettingsError, match="isn't a setting"):
        change(path, "ollama.host", "http://evil.example")  # not in the catalog
    assert path.read_text() == MINE
    path.write_text("[voice]\nspeed = 9\n")  # already broken by hand
    with pytest.raises(SettingsError, match="fix it by hand"):
        change(path, "heads_up.enabled", True)
    assert path.read_text() == "[voice]\nspeed = 9\n"


def test_the_default_template_can_be_changed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_CONFIG_TEMPLATE)
    settings, _ = change(path, "briefing.daily", True)
    assert settings.briefing.daily and path.read_text().startswith(DEFAULT_CONFIG_TEMPLATE.splitlines()[0])


def test_what_the_user_types() -> None:
    assert parse(BY_KEY["heads_up.enabled"], "on") is True
    assert parse(BY_KEY["heads_up.enabled"], "off") is False
    assert parse(BY_KEY["heads_up.hot"], "85") == 85
    assert parse(BY_KEY["cloud.use_for"], "prompts, vision") == ["prompts", "vision"]
    assert parse(BY_KEY["assistant.user_name"], "") is None
    for key, raw in (
        ("heads_up.hot", "500"),
        ("cloud.use_for", "email"),
        ("privacy.cloud", "sure"),
        ("hud.enabled", "y?"),
    ):
        with pytest.raises(SettingsError):
            parse(BY_KEY[key], raw)


def test_what_loosens() -> None:
    assert loosens(BY_KEY["privacy.cloud"], "never", "allow")
    assert not loosens(BY_KEY["privacy.cloud"], "allow", "never")
    assert loosens(BY_KEY["cloud.use_for"], ["prompts"], ["prompts", "vision"])
    assert not loosens(BY_KEY["cloud.use_for"], ["prompts", "vision"], ["prompts"])
    assert loosens(BY_KEY["audit.enabled"], True, False)
    assert loosens(BY_KEY["policy.autonomy"], "confirm-risky", "trusted")


def test_every_entry_is_a_real_setting() -> None:
    for entry in CATALOG:
        value_of(Settings(), entry.key)  # raises if the key doesn't exist


@pytest.fixture
def env(xdg_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    # Never the real sonud: its socket is under XDG_RUNTIME_DIR.
    (tmp_path / "run").mkdir()
    return xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}


def test_sonu_settings_set_asks_audits_and_lists(env: dict[str, str]) -> None:
    runner = CliRunner()
    refused = runner.invoke(app, ["settings", "set", "privacy.cloud", "allow"], env=env)
    assert refused.exit_code == 1 and "needs your OK" in refused.output
    done = runner.invoke(app, ["settings", "set", "privacy.cloud", "allow", "--yes"], env=env)
    assert done.exit_code == 0, done.output
    assert "Cloud models: never → allow — SONU will use it when it next starts" in done.output
    audit = AuditLog(SonuPaths.from_env(env).audit_file)
    [record] = [r for r in audit.records() if r.kind == "settings.change"]
    assert record.data == {"key": "privacy.cloud", "old": "never", "new": "allow"}
    listed = json.loads(runner.invoke(app, ["settings", "list", "--json"], env=env).stdout)
    assert {row["key"]: row["value"] for row in listed}["privacy.cloud"] == "allow"
    unknown = runner.invoke(app, ["settings", "set", "ollama.host", "x"], env=env)
    assert unknown.exit_code == 1
