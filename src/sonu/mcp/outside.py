"""Outside MCP servers' tools as SONU tools (M5 part 3, D63) — untrusted by default.

* The user adds a server in ``[mcp.servers.<name>]`` and says what it reaches (local, web, account);
  SONU can't tell. Its tools become ``mcp.<server>.<tool>`` for the planner, with the server's own
  argument schema (cut down, and checked by SONU before every call).
* Every outside tool is **high risk**: asked every time, with the full arguments shown and the words
  the user didn't say pointed out — except the ones the user listed as ``read_only``. Those run
  without asking on a local server; on a web or account server only when every word they'd send is
  the user's own (otherwise they're asked too).
* What a server answers is **private**: shown to the user (and read out), never given to a model —
  not the reply writer, not the next plan, not a cloud model — because it's text from outside that
  could carry instructions. Kept out of the audit log too (it may be a file or a page).
* A server's "it worked" can't be checked on the desktop: an action is reported *unverified*.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from sonu.config.settings import McpReach, McpServerSettings, McpSettings
from sonu.mcp.client import CallResult, McpConnection, McpError, McpTimeout, RemoteTool, expand_argv
from sonu.mcp.schema import clean_schema, problems, strings_in
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.world.snapshot import WorldSnapshot

logger = logging.getLogger(__name__)

MAX_DESCRIPTION = 200
MAX_SHOWN_ARGS = 4000  # what can be shown in full to approve; longer is refused
MAX_RESULT = 1500
PREFIX = "mcp."
LINK_PARTS = frozenset({"http", "https", "www"})  # not worth pointing out among the words

REACH_WORDS: dict[McpReach, str] = {
    "local": "stays on this laptop",
    "web": "goes to the internet",
    "account": "goes to your online account",
}


class OutsideArgs(BaseModel):
    """The arguments as the server described them; checked by ``OutsideTool.parse_args``."""

    model_config = ConfigDict(extra="allow")


def tool_name(server: str, remote: str) -> str:
    return f"{PREFIX}{server}.{re.sub(r'[^A-Za-z0-9_-]', '_', remote)[:40]}"


def plain(text: str, limit: int) -> str:
    """One line, no control characters, at most ``limit`` characters."""
    return " ".join(text.split())[:limit]


def shown_args(values: Mapping[str, Any]) -> str:
    lines = []
    for name, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{name}: {text}")
    return "\n".join(lines) or "(no arguments)"


def shown_result(text: str) -> str:
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable()).strip()
    if len(text) > MAX_RESULT:
        return f"{text[:MAX_RESULT].rstrip()} … ({len(text) - MAX_RESULT} more characters)"
    return text


class OutsideTool(Tool):
    """One tool of an outside server; ``make_tool`` gives each its own name, description and risk."""

    effects = frozenset({Effect.OUTSIDE})
    Args = OutsideArgs

    def __init__(self, server: OutsideServer, remote: RemoteTool) -> None:
        self.server, self.remote = server, remote
        self.schema = clean_schema(remote.input_schema)

    def parse_args(self, raw: Mapping[str, Any]) -> BaseModel:
        found = problems(dict(raw), self.schema)
        if found:
            raise ValueError("; ".join(found[:3]))
        return OutsideArgs.model_validate(dict(raw))

    def args_json_schema(self) -> dict[str, Any]:
        return self.schema

    @property
    def label(self) -> str:
        return f"{self.remote.name} from the {self.server.name} server"

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        values = args.model_dump()
        reach = self.server.settings.reaches
        shown = shown_args(values)
        if len(shown) > MAX_SHOWN_ARGS:
            return ToolOutcome(
                StepStatus.FAILED,
                f"Didn't use {self.label}: its arguments are too long to show you in full first",
            )
        foreign = sorted(
            {word for text in strings_in(values) for word in ungrounded_words(text, ctx.user_words)}
            - LINK_PARTS
        )
        description = f"Use {self.label} (an outside tool; what it's given {REACH_WORDS[reach]})"
        if self.risk is Risk.READ_ONLY and (reach == "local" or not foreign):
            # The user said it only reads; nothing leaves, or only the user's own words do.
            return Prepared(description, values, risk=None if reach == "local" else Risk.LOW)
        details = f"{shown}\n\nWhat it's given {REACH_WORDS[reach]}."
        if foreign:
            details += f"\nWords you didn't say: {', '.join(foreign[:12])}"
        if self.risk is not Risk.READ_ONLY:
            details += "\nSONU can't check what it does — only what it answers."
        return Prepared(
            description,
            values,
            risk=Risk.HIGH,
            details=details,
            spoken=f"Use {self.remote.name} from {self.server.name}? The details are on screen.",
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        try:
            result = await self.server.call(self.remote.name, prepared.payload)
        except McpError as exc:
            return ToolOutcome(StepStatus.FAILED, f"{self.label} failed: {plain(str(exc), 300)}")
        text = shown_result(result.text)
        if result.is_error:
            return ToolOutcome(StepStatus.FAILED, f"{self.label} said: {text or 'error'}", private=True)
        answer = text or "(it answered nothing)"
        if self.risk is Risk.READ_ONLY:
            return ToolOutcome(StepStatus.DONE, f"{self.label}: {answer}", private=True)
        return ToolOutcome(
            StepStatus.UNVERIFIED,
            f"{self.label} says: {answer} (SONU can't check it)",
            private=True,
        )


def make_tool(server: OutsideServer, remote: RemoteTool) -> OutsideTool:
    read_only = remote.name in server.settings.read_only
    reach = server.settings.reaches
    about = plain(remote.description, MAX_DESCRIPTION) or remote.name
    attributes = {
        "name": tool_name(server.name, remote.name),
        "description": f"From {server.name}, an MCP server the user added ({reach}): {about}",
        "risk": Risk.READ_ONLY if read_only else Risk.HIGH,
    }
    cls = type(f"Outside_{server.name}_{remote.name}", (OutsideTool,), attributes)
    tool: OutsideTool = cls(server, remote)
    return tool


ENV_COMMAND_TIMEOUT_S = 15.0


async def command_env(commands: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Environment values from commands (``gh auth token``): argv, no shell; the value is never logged."""
    env: dict[str, str] = {}
    for name, argv in commands.items():
        try:
            process = await asyncio.create_subprocess_exec(
                *expand_argv(argv),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise McpError(f"couldn't run {argv[0]} for {name}: {exc.strerror or exc}") from exc
        try:
            out, _ = await asyncio.wait_for(process.communicate(), ENV_COMMAND_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise McpError(f"{argv[0]} didn't give {name} in {ENV_COMMAND_TIMEOUT_S:g} s") from None
        value = out.decode(errors="replace").strip()
        if process.returncode != 0 or not value:
            raise McpError(f"{argv[0]} gave no {name} (exit {process.returncode})")
        env[name] = value
    return env


Connect = Callable[[Sequence[str], Mapping[str, str], str], McpConnection]


def _connect(command: Sequence[str], env: Mapping[str, str], label: str) -> McpConnection:
    return McpConnection(command, env=env, label=label)


@dataclass
class OutsideServer:
    name: str
    settings: McpServerSettings
    connect: Connect = _connect
    connection: McpConnection | None = None
    remote: list[RemoteTool] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _start(self) -> McpConnection:
        env = {**self.settings.env, **await command_env(self.settings.env_commands)}
        connection = self.connect(self.settings.command, env, f"the {self.name} server")
        await connection.start()
        return connection

    async def start(self) -> list[OutsideTool]:
        self.connection = await self._start()
        self.remote = await self.connection.list_tools()
        wanted = set(self.settings.tools)
        return [make_tool(self, remote) for remote in self.remote if not wanted or remote.name in wanted]

    async def call(self, name: str, arguments: dict[str, Any]) -> CallResult:
        async with self._lock:
            if self.connection is None or not self.connection.alive:  # it stopped: start it once more
                if self.connection is not None:
                    await self.connection.close()
                self.connection = await self._start()
            try:
                return await self.connection.call_tool(name, arguments, timeout_s=self.settings.timeout_s)
            except McpTimeout:
                # It may still be busy with it (or stuck): stop it; the next call starts it afresh.
                await self.connection.close()
                raise

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()


@dataclass
class OutsideServers:
    """The configured servers once started: their tools, and what went wrong with the others."""

    servers: list[OutsideServer] = field(default_factory=list)
    tools: list[OutsideTool] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)

    async def aclose(self) -> None:
        await asyncio.gather(*(server.close() for server in self.servers))


async def start_servers(settings: McpSettings, connect: Connect = _connect) -> OutsideServers:
    """Start every enabled server at once; one that fails is left out (and said why)."""
    servers = [
        OutsideServer(name, config, connect) for name, config in settings.servers.items() if config.enabled
    ]
    results = await asyncio.gather(*(server.start() for server in servers), return_exceptions=True)
    outside = OutsideServers(servers)
    for server, result in zip(servers, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, McpError):
                logger.error("starting the %s server failed", server.name, exc_info=result)
            outside.problems[server.name] = plain(str(result), 300) or type(result).__name__
            await server.close()
        else:
            outside.tools += result
            missing = set(server.settings.tools) - {remote.name for remote in server.remote}
            if missing:
                outside.problems[server.name] = f"has no tool named {', '.join(sorted(missing))}"
    return outside
