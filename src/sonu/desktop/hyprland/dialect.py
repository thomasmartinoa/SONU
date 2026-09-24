"""Building Hyprland dispatch requests safely, for both config systems.

Hyprland 0.55+ with a Lua config evaluates ``dispatch <x>`` as ``return hl.dispatch(<x>)``;
with a hyprlang config it takes the classic ``dispatch <name> <args>`` form. Both were
verified against the 0.56.2 source.

Two facts from that source shape this module:

* Lua ``dispatch`` *executes code* in the compositor, so nothing here ever interpolates free
  text. Every value is either validated against a strict pattern (workspaces, window selectors)
  or encoded as a Lua string literal in which every non-printable or non-ASCII byte, quote and
  backslash is escaped (``lua_string``).
* When a dispatcher gets **no** window selector it acts on the *focused* window, and when the
  selector matches nothing it silently does nothing and still answers "ok". So every window
  action here *requires* an explicit selector (enforced by the types), and callers must verify
  the result instead of trusting "ok".

Commands for ``exec`` run through ``/bin/sh -c`` inside Hyprland (both ``exec_cmd`` and
``exec_raw`` in 0.56.2), so argv lists are shell-quoted with ``shlex.join``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sonu.desktop.hyprland.errors import HyprlandRequestError
from sonu.desktop.hyprland.models import ConfigProvider, Window, normalize_address

_WORKSPACE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_STABLE_ID = re.compile(r"^[0-9a-fA-F]{1,16}$")
MAX_WORKSPACE_ID = 99


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    """A validated workspace reference: a normal workspace id (1–99) or a plain name."""

    value: str

    @classmethod
    def of(cls, workspace: int | str) -> WorkspaceTarget:
        if isinstance(workspace, bool):
            raise ValueError("workspace must be an int or a name")
        if isinstance(workspace, int):
            if not 1 <= workspace <= MAX_WORKSPACE_ID:
                raise ValueError(f"workspace id must be between 1 and {MAX_WORKSPACE_ID}")
            return cls(str(workspace))
        text = workspace.strip()
        if text.isdigit():
            return cls.of(int(text))
        if not _WORKSPACE_NAME.match(text):
            raise ValueError(f"unsupported workspace name {workspace!r}")
        return cls(text)


@dataclass(frozen=True, slots=True)
class WindowSelector:
    """An exact window selector (never a regex, never "the active window")."""

    value: str

    @classmethod
    def for_window(cls, window: Window, *, prefer_stable_id: bool = True) -> WindowSelector:
        if prefer_stable_id and window.stable_id and _STABLE_ID.match(window.stable_id):
            return cls(f"stableid:{window.stable_id.lower()}")
        return cls(f"address:{normalize_address(window.address)}")

    @classmethod
    def for_address(cls, address: str) -> WindowSelector:
        return cls(f"address:{normalize_address(address)}")


def lua_string(value: str) -> str:
    """Encode text as a Lua string literal that cannot break out of its quotes.

    Only printable ASCII other than quote and backslash is kept as-is; every other byte of the
    UTF-8 encoding becomes a ``\\ddd`` decimal escape, which Lua decodes back to the same bytes.
    """
    out = ['"']
    for byte in value.encode("utf-8"):
        if 0x20 <= byte < 0x7F and byte not in (0x22, 0x5C):
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03d}")
    out.append('"')
    return "".join(out)


def shell_command(argv: Sequence[str]) -> str:
    if not argv or any(not part or "\x00" in part or "\n" in part for part in argv):
        raise HyprlandRequestError("argv must be non-empty strings without NUL or newlines")
    return shlex.join(argv)


class Dialect(Protocol):
    provider: ConfigProvider
    supports_stable_id: bool

    def exec(self, argv: Sequence[str], *, workspace: WorkspaceTarget | None = None) -> str: ...

    def move_to_workspace(self, window: WindowSelector, workspace: WorkspaceTarget) -> str: ...

    def focus_window(self, window: WindowSelector) -> str: ...

    def focus_workspace(self, workspace: WorkspaceTarget) -> str: ...

    def close_window(self, window: WindowSelector) -> str: ...


class LuaDialect:
    provider: ConfigProvider = "lua"
    supports_stable_id = True

    def exec(self, argv: Sequence[str], *, workspace: WorkspaceTarget | None = None) -> str:
        command = lua_string(shell_command(argv))
        if workspace is None:
            return f"dispatch hl.dsp.exec_cmd({command})"
        # "silent": the window opens on that workspace without pulling the user's focus there.
        rules = f"{{ workspace = {lua_string(workspace.value + ' silent')} }}"
        return f"dispatch hl.dsp.exec_cmd({command}, {rules})"

    def move_to_workspace(self, window: WindowSelector, workspace: WorkspaceTarget) -> str:
        # `follow = false` must be explicit: Hyprland only moves silently when it is set to false.
        return (
            "dispatch hl.dsp.window.move({ "
            f"window = {lua_string(window.value)}, "
            f"workspace = {lua_string(workspace.value)}, follow = false }})"
        )

    def focus_window(self, window: WindowSelector) -> str:
        return f"dispatch hl.dsp.focus({{ window = {lua_string(window.value)} }})"

    def focus_workspace(self, workspace: WorkspaceTarget) -> str:
        return f"dispatch hl.dsp.focus({{ workspace = {lua_string(workspace.value)} }})"

    def close_window(self, window: WindowSelector) -> str:
        return f"dispatch hl.dsp.window.close({{ window = {lua_string(window.value)} }})"


class HyprlangDialect:
    """Classic dispatchers (Hyprland < 0.55, or 0.55/0.56 with a hyprlang config).

    Uses ``address:`` selectors only, which every version understands.
    """

    provider: ConfigProvider = "hyprlang"
    supports_stable_id = False

    @staticmethod
    def _legacy_selector(window: WindowSelector) -> str:
        if not window.value.startswith("address:"):
            raise HyprlandRequestError("the classic dialect needs an address: selector")
        return window.value

    def exec(self, argv: Sequence[str], *, workspace: WorkspaceTarget | None = None) -> str:
        command = shell_command(argv)
        if workspace is None:
            return f"dispatch exec {command}"
        return f"dispatch exec [workspace {workspace.value} silent] {command}"

    def move_to_workspace(self, window: WindowSelector, workspace: WorkspaceTarget) -> str:
        return f"dispatch movetoworkspacesilent {workspace.value},{self._legacy_selector(window)}"

    def focus_window(self, window: WindowSelector) -> str:
        return f"dispatch focuswindow {self._legacy_selector(window)}"

    def focus_workspace(self, workspace: WorkspaceTarget) -> str:
        return f"dispatch workspace {workspace.value}"

    def close_window(self, window: WindowSelector) -> str:
        return f"dispatch closewindow {self._legacy_selector(window)}"


def dialect_for(provider: ConfigProvider) -> Dialect:
    return LuaDialect() if provider == "lua" else HyprlangDialect()
