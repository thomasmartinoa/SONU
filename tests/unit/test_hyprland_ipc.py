from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sonu.desktop.hyprland import (
    HyprlandClient,
    HyprlandInstance,
    HyprlandProtocolError,
    HyprlandRequestError,
)
from sonu.testing.fake_hyprland import FakeHyprland, make_window


@pytest.mark.anyio
async def test_version_and_lua_config(fake_hypr: FakeHyprland) -> None:
    client = HyprlandClient(fake_hypr.instance)
    version = await client.version()
    assert version.parsed == (0, 56, 2)
    assert await client.config_provider() == "lua"
    assert fake_hypr.requests[:2] == ["j/version", "j/status"]


@pytest.mark.anyio
async def test_hyprlang_config(short_tmp: Path) -> None:
    async with FakeHyprland(short_tmp, signature="legacy", config_provider="hyprlang") as fake:
        assert await HyprlandClient(fake.instance).config_provider() == "hyprlang"


@pytest.mark.anyio
async def test_pre_lua_hyprland_has_no_status(short_tmp: Path) -> None:
    async with FakeHyprland(short_tmp, signature="old", version="0.54.0", config_provider=None) as fake:
        client = HyprlandClient(fake.instance)
        assert await client.status() is None
        assert await client.config_provider() == "hyprlang"


@pytest.mark.anyio
async def test_clients_workspaces_monitors(fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows = [
        make_window("0xa1", "code-oss", "sonu - Code - OSS", workspace_id=3),
        make_window("0xb2", "chromium", "New Tab - Chromium", workspace_id=2),
        make_window("0xc3", "kitty", "zsh", workspace_id=3),
    ]
    client = HyprlandClient(fake_hypr.instance)
    windows, workspaces, monitors = await asyncio.gather(
        client.clients(), client.workspaces(), client.monitors()
    )
    assert {window.window_class for window in windows} == {"code-oss", "chromium", "kitty"}
    assert {workspace.id: workspace.windows for workspace in workspaces} == {1: 0, 2: 1, 3: 2}
    assert [monitor.name for monitor in monitors] == ["eDP-1"]


@pytest.mark.anyio
async def test_active_window(fake_hypr: FakeHyprland) -> None:
    client = HyprlandClient(fake_hypr.instance)
    assert await client.active_window() is None
    fake_hypr.windows = [make_window("0xa1", "kitty")]
    fake_hypr.active_address = "0xa1"
    active = await client.active_window()
    assert active is not None and active.window_class == "kitty"


@pytest.mark.anyio
async def test_unknown_request_is_a_protocol_error(fake_hypr: FakeHyprland) -> None:
    with pytest.raises(HyprlandProtocolError, match="does not support"):
        await HyprlandClient(fake_hypr.instance).request_json("definitelynotacommand")


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["", " clients", "clients\ndispatch exit", "a\x00b", "x" * 1001])
async def test_malformed_requests_are_refused_before_sending(fake_hypr: FakeHyprland, command: str) -> None:
    with pytest.raises(HyprlandRequestError):
        await HyprlandClient(fake_hypr.instance).request(command)
    assert fake_hypr.requests == []


@pytest.mark.anyio
async def test_missing_socket_is_a_request_error(short_tmp: Path) -> None:
    instance = HyprlandInstance("gone", short_tmp / "hypr" / "gone", "environment")
    with pytest.raises(HyprlandRequestError, match="cannot talk to Hyprland"):
        await HyprlandClient(instance).request("j/version")


@pytest.mark.anyio
async def test_client_half_closes_so_the_compositor_never_waits(short_tmp: Path) -> None:
    """A server that reads until EOF only answers if the client shuts down its write side."""
    directory = short_tmp / "hypr" / "eof"
    directory.mkdir(parents=True)
    received: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read())  # returns only at EOF
        writer.write(b"ok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(serve, path=str(directory / ".socket.sock"))
    try:
        client = HyprlandClient(HyprlandInstance("eof", directory, "environment"), timeout_s=1.0)
        assert await client.request("j/version") == "ok"
        assert received == [b"j/version"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
async def test_silent_server_times_out(short_tmp: Path) -> None:
    directory = short_tmp / "hypr" / "mute"
    directory.mkdir(parents=True)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)
        writer.close()

    server = await asyncio.start_unix_server(serve, path=str(directory / ".socket.sock"))
    try:
        client = HyprlandClient(HyprlandInstance("mute", directory, "environment"), timeout_s=0.2)
        with pytest.raises(HyprlandRequestError, match="did not answer"):
            await client.request("j/version")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
async def test_garbage_json_is_a_protocol_error(fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows = "not a list"  # type: ignore[assignment]
    with pytest.raises(HyprlandProtocolError, match="unexpected shape"):
        await HyprlandClient(fake_hypr.instance).clients()
