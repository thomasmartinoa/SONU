"""Where SONU keeps its files, following the XDG Base Directory specification.

| Kind     | Default                     | Holds                                   |
|----------|-----------------------------|-----------------------------------------|
| config   | ``~/.config/sonu``          | ``config.toml``, routines (later)       |
| data     | ``~/.local/share/sonu``     | memory database (later)                 |
| state    | ``~/.local/state/sonu``     | logs, audit log                         |
| cache    | ``~/.cache/sonu``           | disposable caches                       |
| runtime  | ``$XDG_RUNTIME_DIR/sonu``   | daemon socket (later)                   |

Every directory SONU creates is private to the user (mode ``0700``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "sonu"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _xdg_dir(env: Mapping[str, str], var: str, fallback: Path) -> Path:
    """Resolve one XDG base directory.

    The spec says relative paths in these variables are invalid and must be ignored.
    """
    value = env.get(var, "")
    if value and Path(value).is_absolute():
        return Path(value)
    return fallback


@dataclass(frozen=True, slots=True)
class SonuPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path | None
    config_file: Path

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "sonu.log"

    @property
    def audit_dir(self) -> Path:
        return self.state_dir / "audit"

    @property
    def audit_file(self) -> Path:
        return self.audit_dir / "audit.jsonl"

    @property
    def off_file(self) -> Path:
        """There while the user has turned SONU off (`sonu off`): no request runs until `sonu on`."""
        return self.state_dir / "off"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SonuPaths:
        env = os.environ if env is None else env
        home_value = env.get("HOME", "")
        home = Path(home_value) if home_value else Path.home()

        config_dir = _xdg_dir(env, "XDG_CONFIG_HOME", home / ".config") / APP_NAME
        runtime_base = env.get("XDG_RUNTIME_DIR", "")
        runtime_dir = (
            Path(runtime_base) / APP_NAME if runtime_base and Path(runtime_base).is_absolute() else None
        )
        config_override = env.get("SONU_CONFIG", "")

        return cls(
            config_dir=config_dir,
            data_dir=_xdg_dir(env, "XDG_DATA_HOME", home / ".local" / "share") / APP_NAME,
            state_dir=_xdg_dir(env, "XDG_STATE_HOME", home / ".local" / "state") / APP_NAME,
            cache_dir=_xdg_dir(env, "XDG_CACHE_HOME", home / ".cache") / APP_NAME,
            runtime_dir=runtime_dir,
            config_file=Path(config_override).expanduser() if config_override else config_dir / "config.toml",
        )

    def ensure_private_dirs(self) -> None:
        """Create SONU's persistent directories with private permissions.

        Existing directories keep their mode; SONU never loosens permissions it didn't set.
        """
        for directory in (
            self.config_dir,
            self.data_dir,
            self.state_dir,
            self.cache_dir,
            self.log_dir,
            self.audit_dir,
        ):
            directory.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
