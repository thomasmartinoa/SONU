from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest

from sonu.desktop.hyprland import (
    HyprlandClient,
    HyprlandDispatchError,
    HyprlandRequestError,
    HyprlangDialect,
    LuaDialect,
    Window,
    WindowSelector,
    WorkspaceTarget,
    lua_string,
)
from sonu.testing.fake_dispatch import decode_lua_string, parse_dispatch
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window

NASTY = [
    "",
    'plain "quoted" text',
    "back\\slash",
    'x"); hl.dsp.exit() --',
    "]] .. os.execute('rm -rf ~') .. [[",
    "new\nline\r\x00nul",
    "unicode: café — 🚀",
    "\\065 already escaped",
]


@pytest.mark.parametrize("text", NASTY)
def test_lua_string_round_trips_and_stays_inert(text: str) -> None:
    literal = lua_string(text)
    assert literal.startswith('"') and literal.endswith('"')
    inner = literal[1:-1]
    # Nothing that could end the literal or start Lua code survives unescaped.
    assert '"' not in inner
    assert all(0x20 <= ord(char) < 0x7F for char in inner)
    assert decode_lua_string(literal) == text


def test_workspace_targets_are_validated() -> None:
    assert WorkspaceTarget.of(3).value == "3"
    assert WorkspaceTarget.of("7").value == "7"
    assert WorkspaceTarget.of("dev").value == "dev"
    for bad in (0, 100, -1, True, "special:magic", "two words", "x" * 33, ""):
        with pytest.raises(ValueError, match="workspace"):
            WorkspaceTarget.of(bad)


def test_window_selectors_are_exact() -> None:
    window = Window.model_validate(make_window("0xABC", "kitty", stable_id="1f"))
    assert WindowSelector.for_window(window).value == "stableid:1f"
    assert WindowSelector.for_window(window, prefer_stable_id=False).value == "address:0xabc"
    with pytest.raises(ValueError, match="address"):
        WindowSelector.for_address("class:.*")


def test_lua_dialect_requests() -> None:
    lua = LuaDialect()
    ws3 = WorkspaceTarget.of(3)
    selector = WindowSelector("stableid:1f")
    assert lua.exec(["kitty"]) == 'dispatch hl.dsp.exec_cmd("kitty")'
    assert lua.exec(["code-oss", "--new-window"], workspace=ws3) == (
        'dispatch hl.dsp.exec_cmd("code-oss --new-window", { workspace = "3 silent" })'
    )
    assert lua.move_to_workspace(selector, ws3) == (
        'dispatch hl.dsp.window.move({ window = "stableid:1f", workspace = "3", follow = false })'
    )
    assert lua.focus_window(selector) == 'dispatch hl.dsp.focus({ window = "stableid:1f" })'
    assert lua.focus_workspace(ws3) == 'dispatch hl.dsp.focus({ workspace = "3" })'
    assert lua.close_window(selector) == 'dispatch hl.dsp.window.close({ window = "stableid:1f" })'
    # Names and argument words from Hyprland 0.56.2 (LuaBindingsDispatchers.cpp, parseToggleStr).
    assert lua.set_fullscreen(selector, "fullscreen", True) == (
        'dispatch hl.dsp.window.fullscreen({ mode = "fullscreen", action = "set", window = "stableid:1f" })'
    )
    assert lua.set_fullscreen(selector, "maximized", False) == (
        'dispatch hl.dsp.window.fullscreen({ mode = "maximized", action = "unset", window = "stableid:1f" })'
    )
    assert lua.set_floating(selector, True) == (
        'dispatch hl.dsp.window.float({ action = "enable", window = "stableid:1f" })'
    )
    assert lua.set_pinned(selector, False) == (
        'dispatch hl.dsp.window.pin({ action = "disable", window = "stableid:1f" })'
    )


def test_hyprlang_dialect_requests() -> None:
    legacy = HyprlangDialect()
    ws3 = WorkspaceTarget.of(3)
    selector = WindowSelector("address:0xabc")
    assert legacy.exec(["kitty"], workspace=ws3) == "dispatch exec [workspace 3 silent] kitty"
    assert legacy.move_to_workspace(selector, ws3) == "dispatch movetoworkspacesilent 3,address:0xabc"
    assert legacy.focus_window(selector) == "dispatch focuswindow address:0xabc"
    assert legacy.focus_workspace(ws3) == "dispatch workspace 3"
    assert legacy.close_window(selector) == "dispatch closewindow address:0xabc"
    with pytest.raises(HyprlandRequestError, match="address"):
        legacy.close_window(WindowSelector("stableid:1f"))
    assert legacy.set_floating(selector, True) == "dispatch setfloating address:0xabc"
    assert legacy.set_floating(selector, False) == "dispatch settiled address:0xabc"
    assert legacy.set_pinned(selector, True) == "dispatch pin address:0xabc"
    with pytest.raises(HyprlandRequestError, match="Lua config"):
        legacy.set_fullscreen(selector, "fullscreen", True)


def test_exec_arguments_are_shell_quoted_then_lua_escaped() -> None:
    argv = ["code-oss", "/home/me/My Project", 'x"); hl.dsp.exit() --']
    request = LuaDialect().exec(argv, workspace=WorkspaceTarget.of(2))
    parsed = parse_dispatch(request.removeprefix("dispatch "), "lua")
    assert parsed is not None and parsed.kind == "exec"
    assert shlex.split(parsed.command or "") == argv
    assert parsed.workspace == "2"


@pytest.mark.parametrize("argv", [[], [""], ["kitty", "a\nb"], ["kitty", "a\x00b"]])
def test_bad_argv_is_refused(argv: list[str]) -> None:
    with pytest.raises(HyprlandRequestError):
        LuaDialect().exec(argv)


# ---------------------------------------------------------------- against the fake compositor
async def _settle() -> None:
    await asyncio.sleep(0.15)


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["lua", "hyprlang"])
async def test_exec_honours_workspace_rule(short_tmp: Path, provider: str) -> None:
    async with FakeHyprland(short_tmp, signature=provider, config_provider=provider) as fake:  # type: ignore[arg-type]
        fake.apps["kitty"] = FakeApp("kitty", "zsh")
        dialect = LuaDialect() if provider == "lua" else HyprlangDialect()
        client = HyprlandClient(fake.instance)
        await client.dispatch(dialect.exec(["kitty"], workspace=WorkspaceTarget.of(4)))
        await _settle()
        [window] = await client.clients()
        assert window.window_class == "kitty"
        assert window.workspace.id == 4


@pytest.mark.anyio
async def test_single_instance_app_ignores_rules(fake_hypr: FakeHyprland) -> None:
    fake_hypr.apps["code-oss"] = FakeApp("code-oss", single_instance=True)
    fake_hypr.windows = [make_window("0xa1", "code-oss", workspace_id=1)]
    client = HyprlandClient(fake_hypr.instance)
    await client.dispatch(LuaDialect().exec(["code-oss"], workspace=WorkspaceTarget.of(3)))
    await _settle()
    workspaces = sorted(window.workspace.id for window in await client.clients())
    assert workspaces == [1, 1]  # the new window ignored the rule — exactly why SONU verifies


@pytest.mark.anyio
async def test_move_focus_close(fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows = [make_window("0xa1", "kitty", workspace_id=1, stable_id="a1")]
    client = HyprlandClient(fake_hypr.instance)
    lua = LuaDialect()
    selector = WindowSelector("stableid:a1")
    await client.dispatch(lua.move_to_workspace(selector, WorkspaceTarget.of(5)))
    await _settle()
    assert (await client.clients())[0].workspace.id == 5
    await client.dispatch(lua.focus_window(selector))
    await _settle()
    active = await client.active_window()
    assert active is not None and active.stable_id == "a1"
    await client.dispatch(lua.close_window(selector))
    await _settle()
    assert await client.clients() == []


@pytest.mark.anyio
async def test_unmatched_selector_is_a_silent_ok(fake_hypr: FakeHyprland) -> None:
    """Mirrors real Hyprland: "ok" does not mean anything happened."""
    fake_hypr.windows = [make_window("0xa1", "kitty", stable_id="a1")]
    client = HyprlandClient(fake_hypr.instance)
    await client.dispatch(LuaDialect().close_window(WindowSelector("stableid:ffff")))
    await _settle()
    assert len(await client.clients()) == 1


@pytest.mark.anyio
async def test_dispatch_errors_are_raised(fake_hypr: FakeHyprland) -> None:
    fake_hypr.dispatch_handler = lambda argument: "error: hl.dispatch: window not found"
    client = HyprlandClient(fake_hypr.instance)
    with pytest.raises(HyprlandDispatchError, match="window not found"):
        await client.dispatch(LuaDialect().focus_workspace(WorkspaceTarget.of(2)))
    with pytest.raises(HyprlandRequestError, match="must start with"):
        await client.dispatch("exit")
