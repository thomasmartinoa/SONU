from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sonu.desktop.hyprland import HyprlandNotFound, discover_instance


def _make_instance(runtime: Path, signature: str) -> Path:
    directory = runtime / "hypr" / signature
    directory.mkdir(parents=True)
    (directory / ".socket.sock").touch()
    return directory


def test_discovery_from_environment(tmp_path: Path) -> None:
    directory = _make_instance(tmp_path, "abc_123")
    instance = discover_instance({"XDG_RUNTIME_DIR": str(tmp_path), "HYPRLAND_INSTANCE_SIGNATURE": "abc_123"})
    assert instance.directory == directory
    assert instance.discovered_by == "environment"
    assert instance.event_socket == directory / ".socket2.sock"


def test_stale_signature_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(HyprlandNotFound, match="stale"):
        discover_instance({"XDG_RUNTIME_DIR": str(tmp_path), "HYPRLAND_INSTANCE_SIGNATURE": "gone"})


@pytest.mark.parametrize("signature", ["../etc", "a/b", ".."])
def test_suspicious_signature_is_refused(tmp_path: Path, signature: str) -> None:
    with pytest.raises(HyprlandNotFound, match="suspicious"):
        discover_instance({"XDG_RUNTIME_DIR": str(tmp_path), "HYPRLAND_INSTANCE_SIGNATURE": signature})


def test_falls_back_to_newest_instance(tmp_path: Path) -> None:
    old = _make_instance(tmp_path, "old")
    new = _make_instance(tmp_path, "new")
    past = time.time() - 3600
    os.utime(old / ".socket.sock", (past, past))
    instance = discover_instance({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert instance.directory == new
    assert instance.discovered_by == "newest-instance"


def test_nothing_running(tmp_path: Path) -> None:
    with pytest.raises(HyprlandNotFound, match="does not seem to be running"):
        discover_instance({"XDG_RUNTIME_DIR": str(tmp_path)})
