from __future__ import annotations

import asyncio

import pytest

from sonu.desktop.hyprland import parse_event, stream_events
from sonu.testing.fake_hyprland import FakeHyprland


def test_openwindow_title_may_contain_commas() -> None:
    event = parse_event("openwindow>>55d1a2b3c4d0,3,code-oss,a.py, b.py - sonu - Code - OSS\n")
    assert event is not None
    assert event.name == "openwindow"
    assert event.fields == ("55d1a2b3c4d0", "3", "code-oss", "a.py, b.py - sonu - Code - OSS")
    assert event.window_address == "0x55d1a2b3c4d0"


def test_movewindowv2() -> None:
    event = parse_event("movewindowv2>>abc,2,2")
    assert event is not None
    assert event.fields == ("abc", "2", "2")
    assert event.window_address == "0xabc"


def test_focus_lost_has_no_address() -> None:
    event = parse_event("activewindowv2>>")
    assert event is not None
    assert event.window_address is None


def test_zero_arity_and_unknown_events() -> None:
    reloaded = parse_event("configreloaded>>")
    assert reloaded is not None and reloaded.fields == ()
    future = parse_event("somethingnew>>a,b,c")
    assert future is not None and future.fields == ("a,b,c",)


def test_non_events_are_ignored() -> None:
    assert parse_event("garbage without separator") is None
    assert parse_event(">>no name") is None


def test_workspace_events_have_no_window_address() -> None:
    event = parse_event("workspacev2>>3,3")
    assert event is not None
    assert event.window_address is None


@pytest.mark.anyio
async def test_stream_events_from_fake_compositor(fake_hypr: FakeHyprland) -> None:
    received = []

    async def consume() -> None:
        async for event in stream_events(fake_hypr.instance):
            received.append(event)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await fake_hypr.wait_for_event_clients(1)
    await fake_hypr.emit("openwindow", "abc123,2,chromium,New Tab - Chromium")
    await fake_hypr.emit("movewindowv2", "abc123,3,3")
    async with asyncio.timeout(2):
        await task
    assert [event.name for event in received] == ["openwindow", "movewindowv2"]
    assert received[0].fields[2] == "chromium"
    assert received[1].window_address == "0xabc123"
