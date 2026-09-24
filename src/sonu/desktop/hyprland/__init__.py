"""Hyprland adapter: instance discovery, request client, event stream and typed models."""

from sonu.desktop.hyprland.errors import (
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
    "HyprlandClient",
    "HyprlandError",
    "HyprlandEvent",
    "HyprlandInstance",
    "HyprlandNotFound",
    "HyprlandProtocolError",
    "HyprlandRequestError",
    "HyprlandStatus",
    "HyprlandVersion",
    "Monitor",
    "Window",
    "Workspace",
    "WorkspaceRef",
    "discover_instance",
    "normalize_address",
    "parse_event",
    "stream_events",
]
