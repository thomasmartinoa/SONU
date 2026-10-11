from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonu.audit.log import AuditLog
from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import Engine
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.mcp.server import NOT_OFFERED, PROTOCOL_VERSIONS, McpServer, mcp_name, serve
from sonu.policy import Policy, outside_caller_risk
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry
from sonu.ui.desktop_ui import Notifier, Picker
from tests.conftest import ThreadedFakeHyprland

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class FakePicker(Picker):
    """Says ``answer`` to every question and records what it was asked."""

    answer: bool = True
    asked: list[str] = field(default_factory=list)
    gate: asyncio.Event | None = None  # when set: waits for it before answering
    asking: asyncio.Event = field(default_factory=asyncio.Event)

    async def confirm(self, question: str) -> bool:
        self.asked.append(question)
        self.asking.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.answer


@dataclass(frozen=True)
class FakeNotifier(Notifier):
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send(self, title: str, body: str, *, urgent: bool = False) -> bool:
        self.sent.append((title, body))
        return True


@dataclass
class Rig:
    fake: FakeHyprland
    engine: Engine
    picker: FakePicker
    notifier: FakeNotifier
    audit: AuditLog

    def server(self, *, picker: Picker | bool | None = True) -> McpServer:
        chosen = self.picker if picker is True else (None if picker is False else picker)
        return McpServer(self.engine, chosen, self.notifier, client="claude-code")

    async def call(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = await self.server(**kwargs).handle(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response is not None and response["id"] == 7
        result: dict[str, Any] = response["result"]
        return result


def text(result: dict[str, Any]) -> str:
    return "\n".join(part["text"] for part in result["content"])


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    fake_hypr.apps.update({"kitty": FakeApp("kitty", "zsh"), "zen-browser": FakeApp("zen")})
    fake_hypr.windows = [
        make_window(
            "0xa1", "kitty", "secret-project — zsh", workspace_id=1, stable_id="a1", focus_history_id=0
        ),
        make_window("0xb2", "thunar", "Bank statements", workspace_id=2, stable_id="b2", focus_history_id=1),
    ]
    fake_hypr.active_address = "0xa1"
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(
        desktop,
        AppResolver(catalog, role_defaults={"browser": "zen-browser", "terminal": "kitty"}),
        Settings(apps=AppsSettings(launch_timeout_s=1.5), verify=VerifySettings(timeout_s=1.0)),
        which=lambda name: f"/usr/bin/{name}",
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    engine = Engine(
        planner=ScriptedPlanner({}),
        registry=ToolRegistry(),
        ctx=ctx,
        policy=Policy("confirm-risky"),
        audit=audit,
    )
    yield Rig(fake_hypr, engine, FakePicker("rofi"), FakeNotifier(), audit)


# ---------------------------------------------------------------- the protocol
async def test_initialize_answers_the_clients_version_and_names_it(rig: Rig) -> None:
    server = rig.server()
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "claude-code", "version": "2.1"},
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "sonu"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "never sent to you" in result["instructions"]
    assert server.client == "claude-code"

    unknown = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
    )
    assert unknown is not None and unknown["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


async def test_notifications_get_no_answer_and_unknown_methods_an_error(rig: Rig) -> None:
    server = rig.server()
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert await server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {},
    }
    missing = await server.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert missing is not None and missing["error"]["code"] == -32601
    unknown_tool = await server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "shell_run", "arguments": {}}}
    )
    assert unknown_tool is not None and unknown_tool["error"]["code"] == -32602
    junk = await server.handle(["not", "a", "message"])
    assert junk is not None and junk["error"]["code"] == -32600


async def test_every_tool_but_the_daemons_is_offered_with_its_schema(rig: Rig) -> None:
    response = await rig.server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    expected = {mcp_name(tool.name) for tool in ToolRegistry().tools if tool.name not in NOT_OFFERED}
    assert set(tools) == expected
    assert "voice_wake" not in tools and "dev_prompt" not in tools
    assert all("." not in name for name in tools)  # the Claude API refuses dots in tool names
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
    assert tools["desktop_describe"]["annotations"]["readOnlyHint"] is True
    assert tools["windows_close"]["annotations"]["destructiveHint"] is True
    assert tools["web_search"]["description"].endswith("SONU asks the user on screen first.")
    assert not tools["settings_volume"]["description"].endswith("SONU asks the user on screen first.")


# ---------------------------------------------------------------- calls
async def test_a_local_low_risk_step_runs_and_is_verified_without_asking(rig: Rig) -> None:
    result = await rig.call("apps_launch", {"app": "kitty", "workspace": 3, "new_window": True})
    assert result["isError"] is False, text(result)
    assert text(result).startswith("done: ")
    assert "checked on the desktop afterwards" in text(result)
    assert rig.picker.asked == []
    assert any(w["workspace"]["id"] == 3 for w in rig.fake.windows_of_class("kitty"))

    kinds = [
        (record.kind, record.data.get("caller") or record.data.get("tool")) for record in rig.audit.records()
    ]
    assert kinds == [("call.start", "mcp:claude-code"), ("step.result", "apps.launch")]


async def test_a_search_is_words_the_user_never_said_so_it_is_asked_on_screen(rig: Rig) -> None:
    opened: list[str] = []
    rig.fake.on_url = lambda url, browser: opened.append(url)
    result = await rig.call("web_search", {"query": "arch linux wiki hyprland", "site": "web"})
    [question] = rig.picker.asked
    assert question.startswith("claude-code wants SONU to: ")
    assert "arch linux wiki hyprland" in question
    assert question.endswith("(medium risk). Allow?")
    assert result["isError"] is False, text(result)
    assert opened


async def test_said_no_on_screen_nothing_happens_and_the_client_is_told(rig: Rig) -> None:
    opened: list[str] = []
    rig.fake.on_url = lambda url, browser: opened.append(url)
    picker = FakePicker("rofi", answer=False)
    result = await rig.call("web_search", {"query": "something", "site": "web"}, picker=picker)
    assert len(picker.asked) == 1
    assert result["isError"] is True
    assert text(result).startswith("denied: ")
    assert "The user said no on screen" in text(result)
    assert opened == []


async def test_no_picker_means_nothing_risky_runs(rig: Rig) -> None:
    result = await rig.call("windows_close", {"window": {"app": "thunar"}}, picker=False)
    assert result["isError"] is True
    assert "No on-screen picker" in text(result)
    assert rig.fake.windows_of_class("thunar")


async def test_a_question_too_long_to_show_in_full_is_refused(rig: Rig) -> None:
    long_query = "x" * 150
    result = await rig.call("web_search", {"query": long_query, "site": "web"})
    assert rig.picker.asked == []
    assert result["isError"] is True
    assert "Too long to show the user in full" in text(result)


async def test_personal_content_goes_to_the_user_not_to_the_client(rig: Rig) -> None:
    result = await rig.call("desktop_describe", {})
    assert result["isError"] is False
    said = text(result)
    assert "secret-project" not in said and "Bank statements" not in said
    assert "isn't sent to you" in said
    assert rig.notifier.sent and "Bank statements" in rig.notifier.sent[0][1]
    audited = json.dumps([record.data for record in rig.audit.records()])
    assert "Bank statements" not in audited


async def test_bad_arguments_are_an_error_result_not_a_crash(rig: Rig) -> None:
    result = await rig.call("settings_volume", {"target": "speaker", "level": 400})
    assert result["isError"] is True
    assert "invalid arguments" in text(result)


async def test_a_damaged_audit_log_stops_every_call(rig: Rig) -> None:
    rig.audit.append("doctor.run", {})
    rig.audit.path.write_text(rig.audit.path.read_text().replace("doctor.run", "doctor.ran"))
    result = await rig.call("apps_launch", {"app": "kitty", "new_window": True})
    assert result["isError"] is True
    assert "audit log is damaged" in text(result)
    assert not rig.fake.launched


# ---------------------------------------------------------------- the policy rule
def test_outside_callers_are_asked_for_words_and_for_what_leaves_or_writes() -> None:
    registry = ToolRegistry()

    def risk(tool_name: str, **args: Any) -> Risk | None:
        tool = registry.get(tool_name)
        assert tool is not None
        return outside_caller_risk(tool, args)

    assert risk("settings.volume", target="speaker", level=30) is None
    assert risk("apps.launch", app="kitty") is None
    assert risk("web.search", query="cats") is Risk.MEDIUM  # words the user never said to SONU
    assert risk("web.open", site="github.com") is Risk.MEDIUM  # a link carries data out
    assert risk("files.make_folder", name="x") is Risk.MEDIUM
    assert risk("reminders.set", text="") is None  # no words given: nothing to check
    assert risk("reminders.set", text="stretch") is Risk.MEDIUM


# ---------------------------------------------------------------- stdio
async def test_serve_answers_a_ping_while_a_call_waits_for_the_user(rig: Rig) -> None:
    gate = asyncio.Event()
    picker = FakePicker("rofi", answer=False, gate=gate)
    server = rig.server(picker=picker)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    out: list[dict[str, Any]] = []

    def write(data: bytes) -> None:
        out.append(json.loads(data))

    task = asyncio.create_task(serve(server, incoming.get, write))
    call = {"name": "windows_close", "arguments": {"window": {"app": "thunar"}}}
    await incoming.put(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}).encode()
    )
    await asyncio.wait_for(picker.asking.wait(), 5)
    await incoming.put(b'{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n')
    await incoming.put(b"{not json\n")
    for _ in range(200):
        if len(out) >= 2:
            break
        await asyncio.sleep(0.01)
    assert {"jsonrpc": "2.0", "id": 2, "result": {}} in out  # answered while the call still waits
    assert any(message.get("error", {}).get("code") == -32700 for message in out)
    gate.set()
    await incoming.put(b"")  # the client closed stdin
    await asyncio.wait_for(task, 5)
    assert out[2]["id"] == 1 and out[2]["result"]["isError"] is True


async def test_a_cancelled_call_gets_no_answer(rig: Rig) -> None:
    gate = asyncio.Event()
    picker = FakePicker("rofi", gate=gate)
    server = rig.server(picker=picker)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    out: list[dict[str, Any]] = []
    task = asyncio.create_task(serve(server, incoming.get, lambda data: out.append(json.loads(data))))
    call = {"name": "windows_close", "arguments": {"window": {"app": "thunar"}}}
    await incoming.put(
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": call}).encode()
    )
    await asyncio.wait_for(picker.asking.wait(), 5)
    cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}}
    await incoming.put(json.dumps(cancel).encode())
    await incoming.put(b"")
    await asyncio.wait_for(task, 5)
    assert out == []
    assert rig.fake.windows_of_class("thunar")  # never closed


# ---------------------------------------------------------------- the command
def test_sonu_mcp_serve_over_stdio(
    xdg_env: dict[str, str], tmp_path: Path, threaded_hypr: tuple[ThreadedFakeHyprland, FakeHyprland]
) -> None:
    """The way Claude Code runs it: a child process, JSON-RPC lines on stdin/stdout, nothing else there."""
    _, fake = threaded_hypr
    fake.apps["kitty"] = FakeApp("kitty", "zsh")
    write_desktop_files(Path(xdg_env["XDG_DATA_HOME"]) / "applications")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HYPRLAND_", "XDG_")) and key != "WAYLAND_DISPLAY"
    }
    env |= xdg_env | fake.env | {"XDG_DATA_DIRS": str(tmp_path / "none")}
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "claude-code"},
                "capabilities": {},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "apps_launch",
                "arguments": {"app": "kitty", "workspace": 2, "new_window": True},
            },
        },
    ]
    script = "import sys; from sonu.cli.app import main; sys.argv = ['sonu', 'mcp', 'serve']; main()"
    process = subprocess.run(
        [sys.executable, "-c", script],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines()]  # every line is JSON-RPC
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert len(replies[1]["result"]["tools"]) == len(ToolRegistry().tools) - len(NOT_OFFERED)
    assert replies[2]["result"]["isError"] is False, replies[2]
    assert any(w["workspace"]["id"] == 2 for w in fake.windows_of_class("kitty"))


def test_sonu_mcp_tools_and_setup(xdg_env: dict[str, str]) -> None:
    from typer.testing import CliRunner

    from sonu.cli.app import app

    runner = CliRunner()
    tools = runner.invoke(app, ["mcp", "tools"], env=xdg_env)
    assert tools.exit_code == 0
    lines = {line.split()[0]: line for line in tools.output.splitlines() if line.strip()}
    assert "asks first" in lines["web_search"] and "asks first" in lines["windows_close"]
    assert "low" in lines["settings_volume"]
    assert "voice_wake" not in lines
    setup = runner.invoke(app, ["mcp", "setup"], env=xdg_env)
    assert setup.exit_code == 0
    assert "claude mcp add --scope user sonu --" in setup.output
    assert "mcp serve" in setup.output
