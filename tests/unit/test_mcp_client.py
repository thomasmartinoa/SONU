"""SONU as an MCP client (D63): outside servers' tools, untrusted by default."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sonu.audit.log import AuditLog
from sonu.config.paths import SonuPaths
from sonu.config.settings import McpServerSettings, McpSettings, Settings
from sonu.core.conversation import PRIVATE, Conversation
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.doctor.checks import _mcp_result
from sonu.doctor.model import DoctorContext
from sonu.mcp.client import McpConnection, McpError, server_env
from sonu.mcp.outside import MAX_RESULT, OutsideServers, start_servers
from sonu.mcp.schema import clean_schema, problems
from sonu.mcp.server import offered_tools
from sonu.policy import Policy
from sonu.testing.fake_hyprland import FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.base import Risk, StepStatus
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

pytestmark = pytest.mark.anyio

FAKE = [sys.executable, "-m", "sonu.testing.fake_mcp_server"]
HIDDEN = {"WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "DBUS_SESSION_BUS_ADDRESS", "OPENAI_API_KEY"}


def notes_server(tmp_path: Path, **overrides: Any) -> McpServerSettings:
    config: dict[str, Any] = {
        "command": FAKE,
        "reaches": "local",
        "read_only": ["list_notes"],
        "env": {"FAKE_MCP_NOTES": str(tmp_path / "notes.txt"), "FAKE_MCP_LOG": str(tmp_path / "log.txt")},
        "timeout_s": 3,
    }
    return McpServerSettings.model_validate({**config, **overrides})


# ---------------------------------------------------------------- the arguments' schema
def test_the_schema_is_cut_to_what_sonu_checks() -> None:
    schema = clean_schema(
        {
            "type": "object",
            "title": "Args",
            "properties": {
                "query": {"type": "string", "description": "What\n to   find", "pattern": "^a"},
                "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
                "where": {"$ref": "#/$defs/Where"},
                "loop": {"$ref": "#/$defs/Loop"},
                "either": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "required": ["query", "nothing"],
            "$defs": {
                "Where": {"type": "string", "enum": ["web", "news"]},
                "Loop": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Loop"}}},
            },
        }
    )
    props = schema["properties"]
    assert props["query"] == {"type": "string", "description": "What to find"}  # no pattern: not checked
    assert props["limit"] == {"type": "integer", "minimum": 1, "maximum": 10}
    assert props["where"] == {"type": "string", "enum": ["web", "news"]}
    assert props["either"] == {}  # any value (the server checks it)
    assert schema["required"] == ["query"] and schema["additionalProperties"] is False
    assert len(json.dumps(props["loop"])) < 2000  # a $ref to itself ends
    assert clean_schema("junk") == {"type": "object", "properties": {}, "additionalProperties": False}


def test_arguments_are_checked_against_it() -> None:
    schema = clean_schema(
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 5},
                "limit": {"type": "integer", "maximum": 10},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        }
    )
    assert problems({"query": "arch", "limit": 3, "tags": ["a"]}, schema) == []
    assert problems({}, schema) == ["query: required"]
    assert problems({"query": "arch", "steal": "x"}, schema) == ["steal: not an argument of this tool"]
    assert problems({"query": "arch linux"}, schema) == ["query: wrong length"]
    assert problems({"query": "a", "limit": True}, schema) == ["limit: should be an integer"]  # not a bool
    assert problems({"query": "a", "limit": 11}, schema) == ["limit: out of range"]
    assert problems({"query": "a", "tags": [1]}, schema) == ["tags[0]: should be a string"]


# ---------------------------------------------------------------- talking to a server
async def test_a_server_is_started_listed_and_called(tmp_path: Path) -> None:
    outside = await start_servers(McpSettings(servers={"notes": notes_server(tmp_path)}))
    try:
        assert outside.problems == {}
        names = {tool.name: tool for tool in outside.tools}
        assert "mcp.notes.add_note" in names and "mcp.notes.list_notes" in names
        assert names["mcp.notes.list_notes"].risk is Risk.READ_ONLY  # the user listed it
        # The server says search only reads: shown to the user, never trusted.
        assert names["mcp.notes.search"].risk is Risk.HIGH
        assert names["mcp.notes.search"].remote.read_only_hint is True
        server = outside.servers[0]
        assert (await server.call("add_note", {"text": "milk"})).text == "Saved: milk"
        assert (await server.call("list_notes", {})).text == "milk"
    finally:
        await outside.aclose()
    # It asked SONU for its roots: SONU offers a server nothing.
    assert (tmp_path / "log.txt").read_text() == "roots: refused\n"


async def test_a_server_gets_no_desktop_sockets_or_other_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "WAYLAND_DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "DBUS_SESSION_BUS_ADDRESS",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "secret")
    token = tmp_path / "token.sh"
    token.write_text("#!/bin/sh\necho ' t0ken '\n")
    token.chmod(0o755)
    settings = notes_server(tmp_path, env_commands={"TOKEN": [str(token)]})
    outside = await start_servers(McpSettings(servers={"notes": settings}))
    try:
        text = (await outside.servers[0].call("env_names", {})).text
    finally:
        await outside.aclose()
    names = set(text.split())
    assert {
        "WAYLAND_DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "DBUS_SESSION_BUS_ADDRESS",
        "OPENAI_API_KEY",
    }.isdisjoint(names)
    assert {"PATH", "FAKE_MCP_NOTES"} <= names
    assert "TOKEN=t0ken" in names  # from the command, not the config file
    assert server_env({"A": "1"}, {"PATH": "/bin", "SECRET": "x"}) == {"PATH": "/bin", "A": "1"}


async def test_a_server_that_fails_is_left_out_and_said_why(tmp_path: Path) -> None:
    outside = await start_servers(
        McpSettings(
            servers={
                "notes": notes_server(tmp_path, tools=["list_notes", "frobnicate"]),
                "gone": McpServerSettings(command=[str(tmp_path / "nothing")], reaches="web"),
                "off": McpServerSettings(command=["false"], reaches="web", enabled=False),
                "notoken": notes_server(tmp_path, env_commands={"TOKEN": ["false"]}),
            }
        )
    )
    await outside.aclose()
    assert [tool.name for tool in outside.tools] == ["mcp.notes.list_notes"]  # only the tools asked for
    assert "couldn't start the gone server" in outside.problems["gone"]
    assert outside.problems["notes"] == "has no tool named frobnicate"
    assert "false gave no TOKEN" in outside.problems["notoken"]
    assert "off" not in outside.problems


async def test_a_slow_server_is_stopped_and_a_stopped_one_started_again(tmp_path: Path) -> None:
    outside = await start_servers(McpSettings(servers={"notes": notes_server(tmp_path, timeout_s=0.5)}))
    server = outside.servers[0]
    try:
        with pytest.raises(McpError, match=r"didn't answer in 0\.5 s"):
            await server.call("slow", {})
        assert (await server.call("add_note", {"text": "after"})).text == "Saved: after"  # started afresh
        with pytest.raises(McpError, match="stopped"):
            await server.call("crash", {})
        assert (await server.call("list_notes", {})).text == "after"
    finally:
        await outside.aclose()


async def test_a_program_that_isnt_an_mcp_server(tmp_path: Path) -> None:
    connection = McpConnection(["cat", "/dev/null"], label="cat")
    with pytest.raises(McpError, match="cat"):
        await connection.start(timeout_s=2)


# ---------------------------------------------------------------- through the engine
@dataclass
class Rig:
    ctx: ToolContext
    outside: OutsideServers
    audit: AuditLog
    conversation: Conversation = field(default_factory=Conversation)
    confirmations: list[ConfirmRequest] = field(default_factory=list)
    approve: bool = True

    async def run(self, request: str, tool: str, args: Mapping[str, Any]) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return self.approve

        registry = ToolRegistry()
        registry.add(self.outside.tools)
        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan((tool, dict(args)))}),
            registry=registry,
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
            audit=self.audit,
            conversation=self.conversation,
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    settings = McpSettings(
        servers={
            "notes": notes_server(tmp_path),
            "web": notes_server(tmp_path, reaches="web", read_only=["search"]),
        }
    )
    outside = await start_servers(settings)
    catalog = AppCatalog.load({"HOME": str(tmp_path)}, which=lambda name: None)
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(desktop, AppResolver(catalog), Settings())
    yield Rig(ctx, outside, AuditLog(tmp_path / "audit.jsonl"))
    await outside.aclose()


def summaries(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


async def test_a_read_only_tool_on_a_local_server_runs_unasked_and_stays_private(rig: Rig) -> None:
    await rig.outside.servers[0].call("add_note", {"text": "dentist at 4"})
    report = await rig.run("what notes do I have?", "mcp.notes.list_notes", {})
    assert report.status == "done", summaries(report)
    assert summaries(report) == ["list_notes from the notes server: dentist at 4"]
    assert rig.confirmations == []
    # What a server answers never reaches a model (the next plan sees "(private result)") or the log.
    assert rig.conversation.recent()[0].results == (f"✔ {PRIVATE}",)
    logged = [record.data for record in rig.audit.records() if record.kind == "step.result"]
    assert "dentist" not in json.dumps(logged)


async def test_any_other_tool_is_asked_with_everything_it_would_be_given(rig: Rig, tmp_path: Path) -> None:
    rig.approve = False
    report = await rig.run("save a note: buy oat milk", "mcp.notes.add_note", {"text": "buy oat milk"})
    [asked] = rig.confirmations
    assert asked.risk is Risk.HIGH
    assert asked.details is not None and "text: buy oat milk" in asked.details
    assert "SONU can't check what it does" in asked.details
    assert report.steps[0].outcome.status is StepStatus.DENIED
    assert not (tmp_path / "notes.txt").exists()

    rig.approve = True
    report = await rig.run("save a note: buy oat milk", "mcp.notes.add_note", {"text": "buy oat milk"})
    # Done by the server's word only: unverified, never "done".
    assert report.steps[0].outcome.status is StepStatus.UNVERIFIED
    assert summaries(report) == [
        "add_note from the notes server says: Saved: buy oat milk (SONU can't check it)"
    ]
    assert (tmp_path / "notes.txt").read_text() == "buy oat milk\n"


async def test_a_read_only_tool_that_sends_words_out_runs_unasked_only_with_the_users_words(rig: Rig) -> None:
    report = await rig.run("search the web for arch linux", "mcp.web.search", {"query": "arch linux"})
    assert rig.confirmations == [] and report.status == "done", summaries(report)

    rig.approve = False
    report = await rig.run("search the web for arch linux", "mcp.web.search", {"query": "my bank password"})
    [asked] = rig.confirmations
    assert asked.risk is Risk.HIGH and asked.details is not None
    assert "goes to the internet" in asked.details
    assert "Words you didn't say: bank, password" in asked.details
    assert report.steps[0].outcome.status is StepStatus.DENIED


async def test_a_servers_answer_with_orders_in_it_is_only_shown(rig: Rig) -> None:
    report = await rig.run("search the web for arch linux", "mcp.web.search", {"query": "arch linux"})
    [record] = report.steps
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in record.outcome.summary  # on the user's screen
    assert record.outcome.private  # and nowhere else
    assert rig.conversation.recent()[0].results == (f"✔ {PRIVATE}",)


async def test_wrong_arguments_errors_and_long_answers(rig: Rig) -> None:
    report = await rig.run("save a note", "mcp.notes.add_note", {"text": "x", "steal": "y"})
    assert "invalid arguments (steal: not an argument of this tool)" in summaries(report)[0]
    report = await rig.run("save a note", "mcp.notes.add_note", {"text": "x" * 201})
    assert "text: wrong length" in summaries(report)[0]
    assert rig.confirmations == []  # never asked about a call that can't be made

    report = await rig.run("search the web for fail", "mcp.web.search", {"query": "fail"})
    assert report.steps[0].outcome.status is StepStatus.FAILED
    assert summaries(report) == ["search from the web server said: the search engine is down"]

    report = await rig.run("say a lot", "mcp.notes.big", {})
    summary = summaries(report)[0]
    assert len(summary) < MAX_RESULT + 200 and "more characters)" in summary


# ---------------------------------------------------------------- where outside tools appear
async def test_the_planner_sees_them_and_mcp_clients_dont(rig: Rig) -> None:
    registry = ToolRegistry()
    assert registry.add(rig.outside.tools) == []
    assert registry.add(rig.outside.tools[:1]) == [rig.outside.tools[0].name]  # never twice
    variants = registry.plan_schema()["properties"]["steps"]["items"]["anyOf"]
    [add_note] = [v for v in variants if v["properties"]["tool"]["const"] == "mcp.notes.add_note"]
    assert add_note["properties"]["args"]["required"] == ["text"]
    assert "- mcp.notes.add_note (text): From notes, an MCP server the user added (local): Save a note." in (
        registry.describe_for_planner().splitlines()
    )
    assert not any(name.startswith("mcp_") for name in offered_tools(registry))  # not passed on


def test_server_settings() -> None:
    with pytest.raises(ValidationError, match="reaches"):
        McpServerSettings.model_validate({"command": ["x"]})  # the user must say what it reaches
    with pytest.raises(ValidationError, match="lowercase"):
        McpSettings.model_validate({"servers": {"My Server": {"command": ["x"], "reaches": "web"}}})
    with pytest.raises(ValidationError):
        McpServerSettings.model_validate({"command": [], "reaches": "web"})


def test_the_doctor_line(tmp_path: Path) -> None:
    def doctor(servers: dict[str, Any], which: Any = lambda name: f"/usr/bin/{name}") -> tuple[str, str]:
        ctx = DoctorContext(
            env={},
            paths=SonuPaths.from_env({"HOME": str(tmp_path)}),
            settings=Settings(mcp=McpSettings.model_validate({"servers": servers})),
            settings_from_file=True,
            which=which,
        )
        result = _mcp_result(ctx)
        return result.status, result.summary

    assert doctor({}) == ("info", "none added (see [mcp.servers] in the config)")
    fetch = {"command": ["uvx", "mcp-server-fetch"], "reaches": "web"}
    assert doctor({"fetch": fetch}) == ("ok", "fetch (web) · their tools ask first unless listed read_only")
    assert doctor({"fetch": fetch}, which=lambda name: None) == (
        "fail",
        "can't find the program for fetch (uvx)",
    )
