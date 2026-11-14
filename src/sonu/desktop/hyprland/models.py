"""Typed views of Hyprland's JSON replies (``j/clients``, ``j/workspaces``, ``j/monitors``, ...).

Field names follow Hyprland 0.56.2 (``src/debug/HyprCtl.cpp``). Unknown fields are ignored and
newer fields are optional, so older and newer Hyprland versions still parse.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

ConfigProvider = Literal["lua", "hyprlang"]

_ADDRESS_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{1,16})$")


def normalize_address(value: str) -> str:
    """Canonical window address: lowercase hex with ``0x`` prefix.

    JSON replies use ``0x55d1...`` while socket2 events use ``55d1...``.
    """
    match = _ADDRESS_RE.match(value.strip())
    if not match:
        raise ValueError(f"not a Hyprland window address: {value!r}")
    return "0x" + match.group(1).lower()


Address = Annotated[str, AfterValidator(normalize_address)]


class _HyprModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class WorkspaceRef(_HyprModel):
    id: int
    name: str


class Window(_HyprModel):
    address: Address
    mapped: bool = True
    hidden: bool = False
    at: tuple[int, int]
    size: tuple[int, int]
    workspace: WorkspaceRef
    floating: bool = False
    monitor: int
    window_class: str = Field(alias="class")
    title: str
    initial_class: str = Field(alias="initialClass")
    initial_title: str = Field(alias="initialTitle")
    pid: int
    xwayland: bool = False
    pinned: bool = False
    fullscreen: int = 0
    focus_history_id: int = Field(default=-1, alias="focusHistoryID")
    tags: tuple[str, ...] = ()
    stable_id: str | None = Field(default=None, alias="stableId")

    @property
    def selector(self) -> str:
        """The most precise dispatcher selector for this window."""
        return f"stableid:{self.stable_id}" if self.stable_id else f"address:{self.address}"


class Workspace(_HyprModel):
    id: int
    name: str
    monitor: str
    monitor_id: int | None = Field(default=None, alias="monitorID")
    windows: int
    has_fullscreen: bool = Field(default=False, alias="hasfullscreen")
    last_window: str = Field(default="0x0", alias="lastwindow")
    last_window_title: str = Field(default="", alias="lastwindowtitle")
    is_persistent: bool = Field(default=False, alias="ispersistent")


class Monitor(_HyprModel):
    id: int
    name: str
    description: str = ""
    width: int
    height: int
    refresh_rate: float = Field(alias="refreshRate")
    x: int
    y: int
    active_workspace: WorkspaceRef = Field(alias="activeWorkspace")
    special_workspace: WorkspaceRef | None = Field(default=None, alias="specialWorkspace")
    scale: float
    focused: bool
    disabled: bool = False

    @field_validator("special_workspace", mode="before")
    @classmethod
    def _no_special_workspace(cls, value: object) -> object:
        # Hyprland 0.57 sends {"address": "", "type": "", "name": ""} (no id) when none is open.
        if isinstance(value, dict) and "id" not in value:
            return None
        return value


_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class HyprlandVersion(_HyprModel):
    version: str = ""
    tag: str = ""
    commit: str = ""
    branch: str = ""
    dirty: bool = False

    @property
    def parsed(self) -> tuple[int, int, int] | None:
        for candidate in (self.version, self.tag):
            match = _VERSION_RE.search(candidate)
            if match:
                major, minor, patch = (int(part) for part in match.groups())
                return (major, minor, patch)
        return None

    @property
    def display(self) -> str:
        parsed = self.parsed
        return ".".join(str(part) for part in parsed) if parsed else (self.tag or self.version or "unknown")


class HyprlandStatus(_HyprModel):
    """Reply of ``j/status`` (Hyprland ≥ 0.55)."""

    config_provider: str = Field(alias="configProvider")  # "lua" | "hyprlang" ("error" in theory)
    backend: str = ""
