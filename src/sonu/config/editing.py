"""Changing a setting without editing the file (D90) — what ``sonu settings set`` and the Control Center do.

The config file is edited in place (``tomlkit``: the user's comments and order stay), the whole result is
checked by the same rules as at start-up before anything is written, and it's written atomically (0600). Only
the settings in ``catalog.py`` can be changed this way.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import ValidationError

from sonu.config.catalog import BY_KEY, Entry
from sonu.config.settings import Settings

_TRUE = {"true", "on", "yes", "1"}
_FALSE = {"false", "off", "no", "0"}


class SettingsError(Exception):
    pass


def entry_for(key: str) -> Entry:
    try:
        return BY_KEY[key]
    except KeyError:
        raise SettingsError(
            f"{key!r} isn't a setting that can be changed here (see `sonu settings list`)"
        ) from None


def value_of(settings: Settings, key: str) -> Any:
    value: Any = settings
    for part in key.split("."):
        value = getattr(value, part)
    return list(value) if isinstance(value, list | tuple) else value


def parse(entry: Entry, raw: str) -> Any:
    """A typed value from what the user typed ("on", "12", "prompts,planning", "" for none)."""
    text = raw.strip()
    if entry.kind == "bool":
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise SettingsError(f"{entry.key}: say true or false")
    if entry.kind in ("int", "float"):
        try:
            number: float = int(text) if entry.kind == "int" else float(text)
        except ValueError:
            raise SettingsError(f"{entry.key}: a number, please") from None
        if (entry.minimum is not None and number < entry.minimum) or (
            entry.maximum is not None and number > entry.maximum
        ):
            raise SettingsError(f"{entry.key}: between {entry.minimum:g} and {entry.maximum:g}")
        return number
    if entry.kind == "list":
        items = [item.strip() for item in text.split(",") if item.strip()]
        if bad := [item for item in items if item not in entry.choices]:
            raise SettingsError(f"{entry.key}: {', '.join(bad)} isn't one of {', '.join(entry.choices)}")
        return list(dict.fromkeys(items))
    if entry.kind == "choice" and text not in entry.choices:
        raise SettingsError(f"{entry.key}: one of {', '.join(entry.choices)}")
    return text or None  # "" clears an optional text (your name)


def loosens(entry: Entry, old: Any, new: Any) -> bool:
    """The change gives SONU more reach or keeps more of the user's data: asked "are you sure?" first."""
    if entry.kind == "list":
        return any(item in entry.loosens and item not in (old or []) for item in new)
    return new != old and new in entry.loosens


def change(path: Path, key: str, value: Any) -> tuple[Settings, Any]:
    """Set ``key`` to ``value`` in the config file; returns the new settings and the old value. Nothing is
    written when the result isn't valid."""
    settings, old = change_many(path, {key: value})
    return settings, old[key]


def change_many(path: Path, values: dict[str, Any]) -> tuple[Settings, dict[str, Any]]:
    """Set several settings in one write (a preset, D99): all checked together, all or nothing. Returns the
    new settings and the old values."""
    for key in values:
        entry_for(key)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        document = tomlkit.parse(text)
        old_settings = Settings.model_validate(tomllib.loads(text))
    except (OSError, tomllib.TOMLDecodeError, tomlkit.exceptions.TOMLKitError) as exc:
        raise SettingsError(f"{path}: can't read it ({exc}) — fix it by hand first") from None
    except ValidationError as exc:
        raise SettingsError(
            f"{path} has a problem already — fix it by hand first: {exc.errors()[0]['msg']}"
        ) from None
    for key, value in values.items():
        *tables, name = key.split(".")
        table: Any = document
        for part in tables:
            if part not in table:
                table[part] = tomlkit.table()
            table = table[part]
        if value is None:
            table.pop(name, None)  # back to the default
        else:
            table[name] = value
    new_text = tomlkit.dumps(document)
    try:
        settings = Settings.model_validate(tomllib.loads(new_text))
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error.get("loc", ())) or ", ".join(values)
        raise SettingsError(f"{where}: {error['msg']}") from None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(new_text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return settings, {key: value_of(old_settings, key) for key in values}
