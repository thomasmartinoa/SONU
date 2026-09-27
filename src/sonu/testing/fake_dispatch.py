"""Parsing the dispatch requests SONU generates, for the fake compositor.

This only understands the exact shapes produced by :mod:`sonu.desktop.hyprland.dialect`.
Anything else is reported as an error so tests notice when the two drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_LUA_STR = r'"(?:[^"\\]|\\\d{3})*"'

Kind = Literal["exec", "move", "focus_window", "focus_workspace", "close", "fullscreen", "float", "pin"]

_LUA_PATTERNS: dict[Kind, re.Pattern[str]] = {
    "exec": re.compile(
        rf"^hl\.dsp\.exec_cmd\((?P<cmd>{_LUA_STR})(?:, \{{ workspace = (?P<rule>{_LUA_STR}) \}})?\)$"
    ),
    "move": re.compile(
        rf"^hl\.dsp\.window\.move\(\{{ window = (?P<win>{_LUA_STR}), "
        rf"workspace = (?P<ws>{_LUA_STR}), follow = false \}}\)$"
    ),
    "focus_window": re.compile(rf"^hl\.dsp\.focus\(\{{ window = (?P<win>{_LUA_STR}) \}}\)$"),
    "focus_workspace": re.compile(rf"^hl\.dsp\.focus\(\{{ workspace = (?P<ws>{_LUA_STR}) \}}\)$"),
    "close": re.compile(rf"^hl\.dsp\.window\.close\(\{{ window = (?P<win>{_LUA_STR}) \}}\)$"),
    "fullscreen": re.compile(
        rf"^hl\.dsp\.window\.fullscreen\(\{{ mode = (?P<mode>{_LUA_STR}), "
        rf"action = (?P<action>{_LUA_STR}), window = (?P<win>{_LUA_STR}) \}}\)$"
    ),
    "float": re.compile(
        rf"^hl\.dsp\.window\.float\(\{{ action = (?P<action>{_LUA_STR}), window = (?P<win>{_LUA_STR}) \}}\)$"
    ),
    "pin": re.compile(
        rf"^hl\.dsp\.window\.pin\(\{{ action = (?P<action>{_LUA_STR}), window = (?P<win>{_LUA_STR}) \}}\)$"
    ),
}

_LEGACY_PATTERNS: dict[Kind, re.Pattern[str]] = {
    "exec": re.compile(r"^exec (?:\[workspace (?P<ws>\S+) silent\] )?(?P<cmd>.+)$"),
    "move": re.compile(r"^movetoworkspacesilent (?P<ws>[^,\s]+),(?P<win>\S+)$"),
    "focus_window": re.compile(r"^focuswindow (?P<win>\S+)$"),
    "focus_workspace": re.compile(r"^workspace (?P<ws>\S+)$"),
    "close": re.compile(r"^closewindow (?P<win>\S+)$"),
    "float": re.compile(r"^(?P<action>setfloating|settiled) (?P<win>\S+)$"),
    "pin": re.compile(r"^(?P<action>pin) (?P<win>\S+)$"),
}
_LEGACY_ACTIONS = {"setfloating": "enable", "settiled": "disable", "pin": "toggle"}


@dataclass(frozen=True, slots=True)
class FakeCommand:
    kind: Kind
    command: str | None = None  # shell command for exec
    workspace: str | None = None
    window: str | None = None  # selector, e.g. "stableid:abc" or "address:0x1"
    mode: str | None = None  # fullscreen: "fullscreen" or "maximized"
    action: str | None = None  # set/unset (fullscreen), enable/disable/toggle (float, pin)


def decode_lua_string(literal: str) -> str:
    body = literal[1:-1]
    raw = bytearray()
    index = 0
    while index < len(body):
        if body[index] == "\\":
            raw.append(int(body[index + 1 : index + 4]))
            index += 4
        else:
            raw.extend(body[index].encode("utf-8"))
            index += 1
    return raw.decode("utf-8")


def _group(match: re.Match[str], name: str, provider: Literal["lua", "hyprlang"]) -> str | None:
    raw = match.groupdict().get(name)
    if raw is None:
        return None
    return decode_lua_string(raw) if provider == "lua" else raw


def parse_dispatch(argument: str, provider: Literal["lua", "hyprlang"]) -> FakeCommand | None:
    patterns = _LUA_PATTERNS if provider == "lua" else _LEGACY_PATTERNS
    for kind, pattern in patterns.items():
        match = pattern.match(argument)
        if not match:
            continue
        workspace = _group(match, "ws", provider)
        if kind == "exec" and provider == "lua":
            rule = _group(match, "rule", provider)
            workspace = rule.removesuffix(" silent") if rule else None
        action = _group(match, "action", provider)
        if provider == "hyprlang" and action is not None:
            action = _LEGACY_ACTIONS[action]
        return FakeCommand(
            kind=kind,
            command=_group(match, "cmd", provider),
            workspace=workspace,
            window=_group(match, "win", provider),
            mode=_group(match, "mode", provider),
            action=action,
        )
    return None
