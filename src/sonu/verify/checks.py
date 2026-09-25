"""Verification checks: small, explicit statements about the desktop, evaluated on a snapshot.

A step is only reported as done when every one of its checks passed on a snapshot taken *after*
the action. Each check keeps the evidence it was decided on, which goes into the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sonu.world.snapshot import WindowRef, WorldSnapshot


@dataclass(frozen=True, slots=True)
class Check:
    description: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.description, "passed": self.passed, "evidence": self.evidence}


def window_on_workspace(snapshot: WorldSnapshot, ref: WindowRef, workspace: str, label: str) -> Check:
    window = snapshot.find(ref)
    if window is None:
        return Check(f"{label} is on workspace {workspace}", False, {"found": False})
    return Check(
        f"{label} is on workspace {workspace}",
        window.workspace.name == workspace,
        {"class": window.window_class, "workspace": window.workspace.name},
    )


def window_exists(snapshot: WorldSnapshot, ref: WindowRef, label: str) -> Check:
    window = snapshot.find(ref)
    return Check(
        f"{label} is open",
        window is not None,
        {"class": window.window_class, "workspace": window.workspace.name} if window else {"found": False},
    )


def window_gone(snapshot: WorldSnapshot, ref: WindowRef, label: str) -> Check:
    window = snapshot.find(ref)
    return Check(
        f"{label} is closed",
        window is None,
        {"still_open_on": window.workspace.name} if window else {},
    )


def window_focused(snapshot: WorldSnapshot, ref: WindowRef, label: str) -> Check:
    active = snapshot.active_window
    focused = active is not None and ref.find([active]) is not None
    return Check(
        f"{label} is focused",
        focused,
        {"focused_class": active.window_class if active else None},
    )


def workspace_active(snapshot: WorldSnapshot, workspace: str) -> Check:
    current = snapshot.active_workspace_name
    return Check(f"workspace {workspace} is shown", current == workspace, {"shown": current})
