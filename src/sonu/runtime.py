"""Assembling a ready-to-run engine from the user's settings and environment."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from sonu.audit.log import AuditLog
from sonu.config.paths import SonuPaths
from sonu.config.settings import OllamaSettings, Settings
from sonu.core.conversation import Conversation
from sonu.core.engine import Engine
from sonu.core.memory import Memory
from sonu.core.planner import LLMPlanner
from sonu.core.prompt_writer import PromptWriter
from sonu.core.responder import Responder
from sonu.desktop.apps.catalog import AppCatalog
from sonu.desktop.apps.resolver import AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.desktop.hyprland.errors import HyprlandError
from sonu.desktop.hyprland.instance import discover_instance
from sonu.integrations.hypr_theme import detect_hypr_theme
from sonu.llm.chat import OllamaChatModel
from sonu.llm.ollama import OllamaClient
from sonu.policy.engine import Policy
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


def load_memory(paths: SonuPaths) -> Memory:
    """What the user asked SONU to remember (shared by the CLI and the daemon)."""
    return Memory.load(paths.data_dir / "memory.json")


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


@dataclass
class Runtime:
    engine: Engine
    ollama: OllamaClient
    planner_model: str

    async def aclose(self) -> None:
        await self.ollama.aclose()


async def build_runtime(
    settings: Settings,
    paths: SonuPaths,
    env: Mapping[str, str],
    *,
    deep: bool = False,
    model: str | None = None,
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
        desktop, resolver, settings, default_terminal=role_defaults.get("terminal"), memory=memory
    )

    role = settings.models.planner_deep if deep else settings.models.planner
    if model:
        role = role.model_copy(update={"model": model})
    client = OllamaClient(
        settings.ollama.host, timeout_s=settings.ollama.timeout_s, transport=ollama_transport
    )
    planner = LLMPlanner(OllamaChatModel(client, role, settings.ollama))
    ctx.writer = PromptWriter(OllamaChatModel(client, settings.models.planner, settings.ollama))
    audit = AuditLog(paths.audit_file) if settings.audit.enabled else None
    conversation, responder = assistant_parts(settings, paths, client, memory=memory)
    engine = Engine(
        planner=planner,
        registry=ToolRegistry(),
        ctx=ctx,
        policy=Policy(settings.policy.autonomy),
        audit=audit,
        conversation=conversation,
        responder=responder,
    )
    return Runtime(engine=engine, ollama=client, planner_model=role.model)
