"""Verification: explicit checks evaluated on fresh snapshots, with evidence."""

from sonu.verify.checks import (
    Check,
    window_exists,
    window_focused,
    window_gone,
    window_on_workspace,
    workspace_active,
)

__all__ = [
    "Check",
    "window_exists",
    "window_focused",
    "window_gone",
    "window_on_workspace",
    "workspace_active",
]
