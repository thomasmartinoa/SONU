"""Hyprland adapter: instance discovery, request client, event stream and typed models."""

from sonu.desktop.hyprland.dialect import (
    Dialect,
    HyprlangDialect,
    LuaDialect,
    WindowSelector,
    WorkspaceTarget,
    dialect_for,
    lua_string,
)
from sonu.desktop.hyprland.errors import (
    HyprlandDispatchError,
    HyprlandError,
    HyprlandNotFound,
    HyprlandProtocolError,
    HyprlandRequestError,
)
from sonu.desktop.hyprland.events import HyprlandEvent, parse_event, stream_events
from sonu.desktop.hyprland.instance import HyprlandInstance, discover_instance
from sonu.desktop.hyprland.ipc import HyprlandClient
from sonu.desktop.hyprland.models import (
    ConfigProvider,
    HyprlandStatus,
    HyprlandVersion,
    Monitor,
    Window,
    Workspace,
    WorkspaceRef,
    normalize_address,
)

__all__ = [
    "ConfigProvider",
    "Dialect",
    "HyprlandClient",
    "HyprlandDispatchError",
    "HyprlandError",
    "HyprlandEvent",
    "HyprlandInstance",
    "HyprlandNotFound",
    "HyprlandProtocolError",
    "HyprlandRequestError",
    "HyprlandStatus",
    "HyprlandVersion",
    "HyprlangDialect",
    "LuaDialect",
    "Monitor",
    "Window",
    "WindowSelector",
    "Workspace",
    "WorkspaceRef",
    "WorkspaceTarget",
    "dialect_for",
    "discover_instance",
    "lua_string",
    "normalize_address",
    "parse_event",
    "stream_events",
]
