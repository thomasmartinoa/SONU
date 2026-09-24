from __future__ import annotations

import os
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.markup import escape

from sonu.config import ConfigError, Settings, SonuPaths, load_settings


@dataclass(slots=True)
class AppState:
    paths: SonuPaths
    settings: Settings
    settings_from_file: bool
    settings_error: str | None
    console: Console
    err: Console
    verbosity: int = 0

    @classmethod
    def load(cls, verbosity: int = 0) -> AppState:
        paths = SonuPaths.from_env()
        try:
            loaded = load_settings(paths.config_file)
            settings, from_file, error = loaded.settings, loaded.from_file, None
        except ConfigError as exc:
            settings, from_file, error = Settings(), False, str(exc)
        return cls(
            paths=paths,
            settings=settings,
            settings_from_file=from_file,
            settings_error=error,
            console=Console(),
            err=Console(stderr=True),
            verbosity=verbosity,
        )

    def require_valid_config(self) -> None:
        """Refuse to run with an invalid config.

        Falling back to defaults could silently *loosen* a safety setting the user tightened,
        so everything except `doctor` and `config` stops here.
        """
        if self.settings_error:
            self.err.print(f"[red]error:[/] {escape(self.settings_error)}", highlight=False)
            self.err.print("Fix the file (see `sonu doctor`) or move it aside and run `sonu config init`.")
            raise typer.Exit(code=2)


def get_state(ctx: typer.Context) -> AppState:
    state = ctx.obj
    if not isinstance(state, AppState):  # pragma: no cover - the root callback always sets it
        state = AppState.load()
        ctx.obj = state
    return state


def env_snapshot() -> dict[str, str]:
    return dict(os.environ)
