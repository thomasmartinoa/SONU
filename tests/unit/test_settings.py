from __future__ import annotations

import re
from pathlib import Path

import pytest

from sonu.config import DEFAULT_CONFIG_TEMPLATE, ConfigError, Settings, load_settings


def test_missing_file_means_defaults(tmp_path: Path) -> None:
    loaded = load_settings(tmp_path / "nope.toml")
    assert loaded.settings == Settings()
    assert loaded.from_file is False


def test_values_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[models.planner]\nmodel = "qwen3:8b"\n\n[policy]\nautonomy = "suggest"\n')
    loaded = load_settings(path)
    assert loaded.from_file is True
    assert loaded.settings.models.planner.model == "qwen3:8b"
    assert loaded.settings.policy.autonomy == "suggest"
    # untouched values keep their defaults
    assert loaded.settings.models.embed.model == Settings().models.embed.model


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[policy]\nautonmy = "trusted"\n')
    with pytest.raises(ConfigError, match=r"policy\.autonmy"):
        load_settings(path)


def test_invalid_values_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[policy]\nautonomy = "yolo"\n')
    with pytest.raises(ConfigError, match=r"policy\.autonomy"):
        load_settings(path)


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[policy\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(path)


def test_template_as_written_means_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_CONFIG_TEMPLATE)
    assert load_settings(path).settings == Settings()


def test_template_documents_the_real_defaults(tmp_path: Path) -> None:
    """Uncommenting every `# key = value` line must reproduce the built-in defaults exactly."""
    uncommented = re.sub(r"^# ([a-z_]+ = .+)$", r"\1", DEFAULT_CONFIG_TEMPLATE, flags=re.MULTILINE)
    assert uncommented != DEFAULT_CONFIG_TEMPLATE
    path = tmp_path / "config.toml"
    path.write_text(uncommented)
    assert load_settings(path).settings == Settings()


def test_partial_role_table_keeps_role_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[models.planner]\nthink = true\n")
    settings = load_settings(path).settings
    assert settings.models.planner.think is True
    assert settings.models.planner.model == Settings().models.planner.model
