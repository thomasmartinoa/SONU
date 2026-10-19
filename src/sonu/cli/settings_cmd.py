"""``sonu settings``: see and change SONU's settings without editing the file (D90) — what the Control Center
runs. A change that gives SONU more reach (cloud models, fewer confirmations, …) asks "are you sure?" first;
every change is written to the audit log, and sonud restarts to use it once it's idle."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import sys
from typing import Any

import typer

from sonu.audit.log import AuditError, AuditLog
from sonu.cli.state import get_state
from sonu.config.catalog import CATALOG
from sonu.config.editing import SettingsError, change, change_many, entry_for, loosens, parse, value_of
from sonu.config.presets import LABELS, preset
from sonu.config.presets import current as current_mode
from sonu.daemon.client import ask_reload
from sonu.llm.ollama import OllamaClient, OllamaError
from sonu.llm.providers import BUILT_IN, key_place, lookup_key, store_key
from sonu.runtime import SetupError, provider_for

app = typer.Typer(help="See and change SONU's settings (what the Control Center does).", no_args_is_help=True)


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(map(str, value)) or "none"
    return "not set" if value is None else str(value)


@app.command("list")
def settings_list(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Control Center)."),
) -> None:
    """Every setting that can be changed here, with its current value."""
    state = get_state(ctx)
    state.require_valid_config()
    rows = [
        {
            "key": entry.key,
            "group": entry.group,
            "label": entry.label,
            "help": entry.help,
            "kind": entry.kind,
            "choices": list(entry.choices),
            "min": entry.minimum,
            "max": entry.maximum,
            "loosens": list(entry.loosens),
            "value": value_of(state.settings, entry.key),
        }
        for entry in CATALOG
    ]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    group = ""
    for row in rows:
        if row["group"] != group:
            group = row["group"]
            state.console.print(f"\n[bold]{group}[/]")
        state.console.print(
            f"  {row['label']}: [cyan]{_shown(row['value'])}[/]  [dim]{row['key']}[/]", highlight=False
        )


@app.command("set")
def settings_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="The setting, e.g. heads_up.enabled (see `sonu settings list`)."),
    value: str = typer.Argument(..., help='The new value: on/off, a number, a choice, "" for none.'),
    yes: bool = typer.Option(False, "--yes", help="Already confirmed by the user (the Control Center asks)."),
) -> None:
    """Change one setting; sonud restarts to use it once it's idle."""
    state = get_state(ctx)
    state.require_valid_config()
    try:
        entry = entry_for(key)
        new = parse(entry, value)
        old = value_of(state.settings, key)
        if loosens(entry, old, new) and not yes:
            question = f"{entry.label} → {_shown(new)}. {entry.help} Are you sure?"
            if not sys.stdin.isatty() or not typer.confirm(question, default=False):
                state.err.print(f"Not changed: {entry.label} needs your OK (add --yes once you're sure).")
                raise typer.Exit(1)
        if new == old:
            state.console.print(f"{entry.label} is already {_shown(new)}.")
            return
        _, old = change(state.paths.config_file, key, new)
    except SettingsError as exc:
        state.err.print(f"[red]✖[/] {exc}", highlight=False)
        raise typer.Exit(1) from None
    if state.settings.audit.enabled:  # also when this change turns the audit log off: it's the last record
        try:
            AuditLog(state.paths.audit_file).append("settings.change", {"key": key, "old": old, "new": new})
        except (OSError, AuditError) as exc:
            state.err.print(f"[yellow]▲[/] couldn't write the audit log: {exc}")
    busy = ask_reload()
    when = (
        "SONU will use it when it next starts"
        if busy is None
        else "SONU restarts to use it after the current request"
        if busy
        else "SONU restarts to use it now"
    )
    state.console.print(f"[green]✔[/] {entry.label}: {_shown(old)} → {_shown(new)} — {when}", highlight=False)


@app.command("models")
def settings_models(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Control Center)."),
) -> None:
    """The models installed in Ollama that SONU can plan with."""
    state = get_state(ctx)

    async def installed() -> list[dict[str, Any]]:
        async with OllamaClient(
            state.settings.ollama.host, timeout_s=state.settings.ollama.timeout_s
        ) as client:
            models = await client.installed_models()
            found = []
            for model in models:
                cloud = model.name.endswith(("-cloud", ":cloud"))
                try:
                    tools = (await client.model_info(model.name)).supports_tools
                except OllamaError:
                    tools = False
                found.append(
                    {"name": model.name, "gb": round(model.size / 1024**3, 1), "cloud": cloud, "tools": tools}
                )
            return found

    try:
        models = asyncio.run(installed())
    except OllamaError as exc:
        state.err.print(f"[red]✖[/] Ollama: {exc}")
        raise typer.Exit(1) from None
    if as_json:
        print(json.dumps(models))
        return
    for model in models:
        where = "cloud" if model["cloud"] else f"{model['gb']} GB"
        usable = "" if model["tools"] else "  [dim](can't plan: no tool calling)[/]"
        state.console.print(f"  {model['name']}  [dim]{where}[/]{usable}", highlight=False)


@app.command("key")
def settings_key(
    ctx: typer.Context,
    provider: str = typer.Argument(
        ..., help="anthropic, gemini, groq, openrouter, omniroute, or one of yours"
    ),
    from_stdin: bool = typer.Option(False, "--stdin", help="Read the key from standard input (the window)."),
) -> None:
    """Keep a cloud provider's API key in the system keyring — or, with no keyring, a private file (0600) —
    never in the settings file (D94)."""
    state = get_state(ctx)
    state.require_valid_config()
    try:
        found = provider_for(state.settings, provider)
    except SetupError as exc:
        state.err.print(f"[red]✖[/] {exc}")
        raise typer.Exit(1) from None
    key = (
        sys.stdin.readline().strip()
        if from_stdin
        else getpass.getpass(f"{found.label} API key (hidden): ").strip()
    )
    if not key:
        state.err.print("Nothing stored (no key given).")
        raise typer.Exit(1)
    where = store_key(found.name, key, os.environ)
    if where is None:
        state.err.print("[red]✖[/] couldn't keep it anywhere (no keyring, and no home folder)")
        raise typer.Exit(1)
    if state.settings.audit.enabled:  # that a key was set — never the key
        with contextlib.suppress(OSError, AuditError):
            AuditLog(state.paths.audit_file).append("settings.key", {"provider": found.name})
    state.console.print(f"[green]✔[/] {found.label}'s key is kept in {where}.", highlight=False)


@app.command("providers")
def settings_providers(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Control Center)."),
) -> None:
    """The cloud providers SONU can use, and whether each has a key (D94)."""
    state = get_state(ctx)
    state.require_valid_config()
    names = ["ollama", *dict.fromkeys([*BUILT_IN, *state.settings.providers])]
    rows = []
    for name in names:
        if name == "ollama":
            rows.append({"name": "ollama", "label": "Ollama Cloud", "kind": "ollama", "key": True})
            continue
        found = provider_for(state.settings, name)
        key = lookup_key(found, os.environ)
        rows.append(
            {
                "name": name,
                "label": found.label,
                "kind": found.kind,
                "key": key is not None,
                # Found on real hardware: a saved key looked lost — the box was empty again after a restart.
                # Its last 4 characters show it's there (never more of it), and where it's kept.
                "ends": key[-4:] if key and len(key) >= 12 else "",
                "where": key_place(found, os.environ) if key else "",
            }
        )
    if as_json:
        print(json.dumps(rows))
        return
    for row in rows:
        mark = "[green]key ✔[/]" if row["key"] else f"[dim]no key — sonu settings key {row['name']}[/]"
        if row["name"] == "ollama":
            mark = "[dim]`ollama signin`[/]"
        state.console.print(f"  {row['name']:<11} {row['label']:<28} {mark}", highlight=False)


@app.command("preset")
def settings_preset(
    ctx: typer.Context,
    name: str = typer.Argument("", help="local, balanced or cloud; none: show which one you're on"),
    yes: bool = typer.Option(False, "--yes", help="Already confirmed by the user (the Control Center asks)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Control Center)."),
) -> None:
    """Fully local, Balanced or Full cloud in one go (D99): every related setting, checked together."""
    state = get_state(ctx)
    state.require_valid_config()
    if not name:
        mode = current_mode(state.settings)
        print(json.dumps({"mode": mode, "label": LABELS[mode]})) if as_json else state.console.print(
            LABELS[mode]
        )
        return

    def has_key(provider: str) -> bool:
        return lookup_key(provider_for(state.settings, provider), os.environ) is not None

    try:
        values, notes = preset(name, has_key)
    except ValueError as exc:
        state.err.print(f"[red]✖[/] {exc}")
        raise typer.Exit(2) from None
    old = {key: value_of(state.settings, key) for key in values}
    more = [key for key, value in values.items() if loosens(entry_for(key), old[key], value)]
    if more and not yes:
        question = f"{LABELS[name]}: more goes to the cloud ({', '.join(more)}). Are you sure?"
        if not sys.stdin.isatty() or not typer.confirm(question, default=False):
            state.err.print(f"Not changed: {LABELS[name]} needs your OK (add --yes once you're sure).")
            raise typer.Exit(1)
    try:
        _, old = change_many(state.paths.config_file, values)
    except SettingsError as exc:
        state.err.print(f"[red]✖[/] {exc}", highlight=False)
        raise typer.Exit(1) from None
    if state.settings.audit.enabled:
        with contextlib.suppress(OSError, AuditError):
            AuditLog(state.paths.audit_file).append(
                "settings.preset", {"preset": name, "old": old, "new": values}
            )
    busy = ask_reload()
    when = "SONU will use it when it next starts" if busy is None else "SONU restarts to use it"
    state.console.print(f"[green]✔[/] {LABELS[name]} — {when}", highlight=False)
    for note in notes:
        state.console.print(f"  [yellow]▲[/] {note}", highlight=False)
