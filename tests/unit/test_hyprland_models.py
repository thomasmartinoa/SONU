from __future__ import annotations

import pytest

from sonu.desktop.hyprland import HyprlandVersion, Monitor, Window, normalize_address
from sonu.testing.fake_hyprland import make_monitor, make_window


def test_window_parses_hyprland_0_56_json() -> None:
    raw = make_window(
        "0x55D1A2B3C4D0", "code-oss", "main.py - sonu - Code - OSS", workspace_id=3, stable_id="1a2b"
    )
    window = Window.model_validate(raw)
    assert window.address == "0x55d1a2b3c4d0"
    assert window.window_class == "code-oss"
    assert window.workspace.id == 3
    assert window.stable_id == "1a2b"
    assert window.selector == "stableid:1a2b"


def test_window_without_stable_id_uses_address_selector() -> None:
    raw = make_window("0xabc", "kitty")
    del raw["stableId"]  # older Hyprland versions
    window = Window.model_validate(raw)
    assert window.stable_id is None
    assert window.selector == "address:0xabc"


def test_unknown_fields_are_ignored() -> None:
    raw = make_window("0xabc", "kitty") | {"someFutureField": {"nested": True}}
    assert Window.model_validate(raw).window_class == "kitty"


def test_monitor_parses() -> None:
    monitor = Monitor.model_validate(make_monitor("eDP-1", active_workspace=2))
    assert monitor.name == "eDP-1"
    assert monitor.active_workspace.id == 2
    assert monitor.scale == pytest.approx(1.6)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("55d1a2b3c4d0", "0x55d1a2b3c4d0"), ("0x55D1", "0x55d1"), (" 0xabc ", "0xabc")],
)
def test_normalize_address(raw: str, expected: str) -> None:
    assert normalize_address(raw) == expected


@pytest.mark.parametrize("raw", ["", "0x", "xyz", "0x" + "f" * 17, "12 34"])
def test_normalize_address_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError, match="address"):
        normalize_address(raw)


def test_version_parsing() -> None:
    version = HyprlandVersion.model_validate({"version": "0.56.2", "tag": "v0.56.2", "commit": "efb5099"})
    assert version.parsed == (0, 56, 2)
    assert version.display == "0.56.2"
    assert HyprlandVersion.model_validate({"tag": "v0.54.1-b"}).parsed == (0, 54, 1)
    assert HyprlandVersion.model_validate({"tag": "weird"}).parsed is None
