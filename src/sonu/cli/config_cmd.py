from __future__ import annotations

import os
from datetime import datetime

import typer

from sonu.cli.state import get_state
from sonu.config import DEFAULT_CONFIG_TEMPLATE
from sonu.config.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE

app = typer.Typer(help="Show or create SONU's configuration file.", no_args_is_help=True)


@app.command("path")
def config_path(ctx: typer.Context) -> None:
    """Print where SONU reads its config from."""
    state = get_state(ctx)
    state.console.print(str(state.paths.config_file), highlight=False)


@app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective configuration (file values merged over defaults)."""
    state = get_state(ctx)
    if state.settings_error:
        state.require_valid_config()
    source = str(state.paths.config_file) if state.settings_from_file else "built-in defaults"
    state.err.print(f"[dim]# source: {source}[/]", highlight=False)
    state.console.print_json(state.settings.model_dump_json())


@app.command("init")
def config_init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file (a backup is kept)."),
) -> None:
    """Write a commented default config file."""
    state = get_state(ctx)
    path = state.paths.config_file
    path.parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if path.exists():
        if not force:
            state.err.print(f"{path} already exists. Use --force to replace it (a backup is kept).")
            raise typer.Exit(code=1)
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        path.rename(backup)
        state.console.print(f"backed up the old file to {backup}", highlight=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(DEFAULT_CONFIG_TEMPLATE)
    state.console.print(f"wrote {path}", highlight=False)
