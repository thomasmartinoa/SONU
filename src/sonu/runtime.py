"""Assembling a ready-to-run engine from the user's settings and environment."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from sonu.audit.log import AuditLog
from sonu.browser.host import socket_folder
from sonu.config.paths import SonuPaths
from sonu.config.settings import CloudUse, ModelRole, OllamaSettings, Settings
from sonu.core.conversation import Conversation
from sonu.core.engine import Engine
from sonu.core.journal import Journal
from sonu.core.layouts import Layouts
from sonu.core.memory import Memory
from sonu.core.page_reader import PageReader, page_chars
from sonu.core.planner import LLMPlanner, RoutedPlanner
from sonu.core.planning import Planner
from sonu.core.prompt_writer import PromptWriter
from sonu.core.responder import Responder
from sonu.core.screen_reader import ScreenReader
from sonu.core.talks import Talks
from sonu.desktop.apps.catalog import AppCatalog
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.instance import discover_instance
from sonu.integrations.browser import Browsers
from sonu.integrations.hypr_theme import detect_hypr_theme
from sonu.integrations.mpv import SonuPlayer, music_folder
from sonu.integrations.research import WebAccess
from sonu.llm.chat import OllamaChatModel, TextModel
from sonu.llm.cloud import (
    PAUSE_FILE,
    CloudModel,
    CloudPause,
    CloudRefused,
    LocalFallback,
    check_local_roles,
    cloud_allowed,
    is_cloud_model,
)
from sonu.llm.ollama import OllamaClient
from sonu.mcp.outside import OutsideServers, start_servers
from sonu.policy.engine import Policy
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def load_memory(paths: SonuPaths) -> Memory:
    """What the user asked SONU to remember (shared by the CLI and the daemon)."""
    return Memory.load(paths.data_dir / "memory.json")


def load_layouts(paths: SonuPaths) -> Layouts:
    """Saved window layouts (shared by the CLI and the daemon; re-read before each use)."""
    return Layouts.load(paths.data_dir / "layouts.json")


def open_journal(settings: Settings, paths: SonuPaths) -> Journal | None:
    """The activity journal (sonud records it; the CLI only reads it). None when it's off in the config."""
    if not settings.journal.enabled:
        return None
    return Journal(
        paths.data_dir / "journal.sqlite3",
        keep_days=settings.journal.keep_days,
        exclude_apps=settings.journal.exclude_apps,
        pause_file=paths.data_dir / "journal-paused",
    )


def open_talks(settings: Settings, paths: SonuPaths) -> Talks | None:
    """What the user and SONU said to each other (D86). None when it's off in the config."""
    if not settings.conversations.keep:
        return None
    return Talks(paths.data_dir / "conversations.sqlite3", keep_days=settings.conversations.keep_days)


def app_aliases(settings: Settings, memory: Memory) -> dict[str, str]:
    """The config's aliases, and what the user said with "remember …" (newer, so it wins)."""
    return {**settings.apps.aliases, **memory.aliases()}


def assistant_parts(
    settings: Settings,
    paths: SonuPaths,
    client: OllamaClient,
    ollama: OllamaSettings | None = None,
    memory: Memory | None = None,
) -> tuple[Conversation, Responder | None]:
    """The conversation so far (shared by the CLI and the daemon) and the reply writer."""
    assistant = settings.assistant
    conversation = Conversation.load(
        paths.state_dir / "conversation.json", idle_s=assistant.conversation_minutes * 60
    )
    if not assistant.replies:
        return conversation, None
    model = OllamaChatModel(client, settings.models.planner, ollama or settings.ollama)
    user_name = (memory.name() if memory is not None else None) or assistant.user_name
    return conversation, Responder(model, name=assistant.name, user_name=user_name)


class SetupError(Exception):
    """SONU cannot start: something it depends on is missing or unreachable."""


@dataclass(frozen=True)
class ModelParts:
    planner: Planner
    local_planner: LLMPlanner  # sonud warms this one up
    planner_model: str  # what the CLI shows
    writer: PromptWriter
    reader: ScreenReader
    pages: PageReader  # always the local model (D71)


def model_parts(
    settings: Settings,
    client: OllamaClient,
    ollama: OllamaSettings,
    audit: AuditLog | None,
    *,
    deep: bool = False,
    model: str | None = None,
    state_dir: Path | None = None,  # where a usage-limit pause is kept (None: in memory)
) -> ModelParts:
    """The planner, prompt writer and screen reader — local, or Ollama Cloud for the uses the user
    allowed (D62). Shared by the CLI and sonud."""
    try:
        check_local_roles(settings)
    except CloudRefused as exc:
        raise SetupError(str(exc)) from exc
    role = settings.models.planner_deep if deep else settings.models.planner
    if model:
        role = role.model_copy(update={"model": model})

    pause = CloudPause(state_dir / PAUSE_FILE if state_dir is not None else None)

    def cloud(use: CloudUse, name: str) -> CloudModel:
        cloud_role = ModelRole(model=name, think=settings.cloud.think)
        return CloudModel(OllamaChatModel(client, cloud_role, ollama), use, audit, pause)

    local = LLMPlanner(OllamaChatModel(client, role, ollama))
    planner: Planner = local
    shown = role.model
    if is_cloud_model(role.model):  # `sonu --model gpt-oss:120b-cloud …`
        if not cloud_allowed(settings, "planning"):
            raise SetupError(
                f'{role.model} is a cloud model: set [privacy] cloud = "allow" and add "planning" to '
                "[cloud] use_for"
            )
        local = LLMPlanner(OllamaChatModel(client, settings.models.planner, ollama))
        planner = LLMPlanner(cloud("planning", role.model), include_titles=False)
    elif cloud_allowed(settings, "planning"):
        remote = LLMPlanner(cloud("planning", settings.cloud.model), include_titles=False)
        if deep:  # the cloud one first; the local deep model if it can't
            planner, shown = RoutedPlanner(local, remote, always=True), settings.cloud.model
        else:
            planner = RoutedPlanner(local, remote, always=settings.cloud.plan == "always")
            shown = f"{role.model} (+ {settings.cloud.model} for harder requests)"
    local_text = OllamaChatModel(client, settings.models.planner, ollama)
    writer: TextModel = (
        LocalFallback(cloud("prompts", settings.cloud.model), local_text)
        if cloud_allowed(settings, "prompts")
        else local_text
    )
    if cloud_allowed(settings, "vision"):
        vision = LocalFallback(cloud("vision", settings.cloud.vision_model), local_text)
        reader = ScreenReader(vision, settings.ollama.host, cloud=True)
    else:
        reader = ScreenReader(local_text, settings.ollama.host)
    pages = PageReader(local_text, settings.ollama.host, page_chars(settings.ollama.num_ctx))
    return ModelParts(planner, local, shown, PromptWriter(writer), reader, pages)


async def outside_tools(settings: Settings, registry: ToolRegistry) -> OutsideServers | None:
    """Start the MCP servers the user added (D63) and give the planner their tools. None: there are none."""
    if not any(server.enabled for server in settings.mcp.servers.values()):
        return None
    outside = await start_servers(settings.mcp)
    for name in registry.add(outside.tools):
        logger.warning("an outside tool has the name of another one: %s (left out)", name)
    for name, problem in outside.problems.items():
        logger.warning("MCP server %s: %s", name, problem)
    return outside


@dataclass
class Runtime:
    engine: Engine
    ollama: OllamaClient
    planner_model: str
    outside: OutsideServers | None = None

    async def aclose(self) -> None:
        if self.outside is not None:
            await self.outside.aclose()
        await self.ollama.aclose()
        if self.engine.ctx.journal is not None:
            self.engine.ctx.journal.close()
        if self.engine.ctx.talks is not None:
            self.engine.ctx.talks.close()


async def build_runtime(
    settings: Settings,
    paths: SonuPaths,
    env: Mapping[str, str],
    *,
    deep: bool = False,
    model: str | None = None,
    outside: bool = True,  # the MCP servers the user added (not for `sonu mcp serve`)
    ollama_transport: httpx.AsyncBaseTransport | None = None,  # tests plug in a fake Ollama here
) -> Runtime:
    try:
        instance = discover_instance(env)
        desktop = await HyprlandDesktop.connect(instance, timeout_s=settings.hyprland.request_timeout_s)
    except HyprlandError as exc:
        raise SetupError(f"{exc}\n→ run SONU inside your Hyprland session (see `sonu doctor`)") from exc

    catalog = await asyncio.to_thread(AppCatalog.load, env)
    rice = detect_hypr_theme(Path(env.get("HOME") or Path.home()))
    role_defaults = {kind: app.command for kind, app in rice.defaults.items()}
    memory = load_memory(paths)
    resolver = AppResolver(catalog, role_defaults=role_defaults, aliases=app_aliases(settings, memory))
    ctx = ToolContext(
        desktop,
        resolver,
        settings,
        default_terminal=role_defaults.get("terminal"),
        memory=memory,
        layouts=load_layouts(paths),
        browsers=Browsers(socket_folder(dict(env))),
        music=SonuPlayer(music_folder(dict(env))),
        web=WebAccess(),
        journal=open_journal(settings, paths),
        talks=open_talks(settings, paths),
    )

    client = OllamaClient(
        settings.ollama.host, timeout_s=settings.ollama.timeout_s, transport=ollama_transport
    )
    audit = AuditLog(paths.audit_file) if settings.audit.enabled else None
    try:
        parts = model_parts(
            settings, client, settings.ollama, audit, deep=deep, model=model, state_dir=paths.state_dir
        )
    except SetupError:
        await client.aclose()
        raise
    ctx.writer, ctx.reader, ctx.pages = parts.writer, parts.reader, parts.pages
    conversation, responder = assistant_parts(settings, paths, client, memory=memory)
    registry = ToolRegistry()
    servers = await outside_tools(settings, registry) if outside else None
    engine = Engine(
        planner=parts.planner,
        registry=registry,
        ctx=ctx,
        policy=Policy(settings.policy.autonomy),
        audit=audit,
        conversation=conversation,
        responder=responder,
    )
    return Runtime(engine=engine, ollama=client, planner_model=parts.planner_model, outside=servers)
