from __future__ import annotations

import stat
from pathlib import Path

from sonu.config.paths import SonuPaths


def test_defaults_follow_home() -> None:
    paths = SonuPaths.from_env({"HOME": "/home/alice"})
    assert paths.config_dir == Path("/home/alice/.config/sonu")
    assert paths.data_dir == Path("/home/alice/.local/share/sonu")
    assert paths.state_dir == Path("/home/alice/.local/state/sonu")
    assert paths.cache_dir == Path("/home/alice/.cache/sonu")
    assert paths.config_file == Path("/home/alice/.config/sonu/config.toml")
    assert paths.audit_file == Path("/home/alice/.local/state/sonu/audit/audit.jsonl")
    assert paths.runtime_dir is None


def test_xdg_variables_are_respected() -> None:
    paths = SonuPaths.from_env(
        {
            "HOME": "/home/alice",
            "XDG_CONFIG_HOME": "/cfg",
            "XDG_STATE_HOME": "/state",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
    )
    assert paths.config_dir == Path("/cfg/sonu")
    assert paths.state_dir == Path("/state/sonu")
    assert paths.runtime_dir == Path("/run/user/1000/sonu")


def test_relative_xdg_paths_are_ignored() -> None:
    # The XDG spec says relative paths are invalid and must be ignored.
    paths = SonuPaths.from_env({"HOME": "/home/alice", "XDG_CONFIG_HOME": "relative/cfg"})
    assert paths.config_dir == Path("/home/alice/.config/sonu")


def test_sonu_config_override() -> None:
    paths = SonuPaths.from_env({"HOME": "/home/alice", "SONU_CONFIG": "/etc/sonu-test.toml"})
    assert paths.config_file == Path("/etc/sonu-test.toml")


def test_ensure_private_dirs_creates_0700_dirs(xdg_env: dict[str, str]) -> None:
    paths = SonuPaths.from_env(xdg_env)
    paths.ensure_private_dirs()
    for directory in (paths.config_dir, paths.data_dir, paths.state_dir, paths.log_dir, paths.audit_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
