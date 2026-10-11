"""SONU's configuration: a single TOML file validated by pydantic.

Unknown keys are rejected (``extra="forbid"``) so a typo like ``autonmy = "trusted"``
fails loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

AutonomyMode = Literal["suggest", "confirm-each", "confirm-risky", "trusted"]
CloudPolicy = Literal["never", "ask", "allow"]
CloudUse = Literal["prompts", "planning", "vision"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OllamaSettings(_Section):
    host: str = "http://127.0.0.1:11434"
    timeout_s: float = Field(default=10.0, gt=0, le=600)
    # Generation can include loading the model into GPU memory on first use.
    chat_timeout_s: float = Field(default=120.0, gt=0, le=1800)
    keep_alive: str = "10m"
    # The planner's instructions are ~7,000 tokens; at 8,192 a busy desktop (~40 windows) left no room for
    # the plan, and more made Ollama silently drop the start of the prompt (measured 2026-10-12). 16,384
    # costs ~0.3 GB more GPU memory for qwen3.5:4b (3.3 → 3.6 GB).
    num_ctx: int = Field(default=16384, ge=2048, le=262144)


class ModelRole(_Section):
    """Which model serves a role. Only the Ollama provider exists in milestone 1."""

    provider: Literal["ollama"] = "ollama"
    model: str = Field(min_length=1)
    think: bool = False


# One class per role so a partial table (e.g. only `think = true`) keeps that role's defaults.
class PlannerRole(ModelRole):
    model: str = Field(default="qwen3.5:4b", min_length=1)


class DeepPlannerRole(ModelRole):
    model: str = Field(default="qwen3.5:9b", min_length=1)
    think: bool = True


class EmbedRole(ModelRole):
    model: str = Field(default="qwen3-embedding:0.6b", min_length=1)


class ModelsSettings(_Section):
    planner: PlannerRole = PlannerRole()
    planner_deep: DeepPlannerRole = DeepPlannerRole()
    embed: EmbedRole = EmbedRole()


class PolicySettings(_Section):
    autonomy: AutonomyMode = "confirm-risky"


class PrivacySettings(_Section):
    cloud: CloudPolicy = "never"


class HyprlandSettings(_Section):
    request_timeout_s: float = Field(default=2.0, gt=0, le=30)


class AppsSettings(_Section):
    # Your words -> desktop-file ID or executable, e.g. {"vs code" = "codium.desktop"}.
    aliases: dict[str, str] = Field(default_factory=dict)
    # How long to wait for a launched app's window (cold starts of big apps can be slow).
    launch_timeout_s: float = Field(default=20.0, gt=0, le=120)
    # "Open X" when X is already open: bring/move the existing window instead of a new one.
    reuse_existing: bool = True


SearchEngine = Literal["duckduckgo", "google", "brave", "startpage"]


class WebSettings(_Section):
    # Where "search the web for …" goes. Site searches (YouTube, GitHub, …) always use that site.
    search_engine: SearchEngine = "duckduckgo"
    # How long a page may take to load before SONU reports it as unconfirmed.
    load_timeout_s: float = Field(default=30.0, gt=0, le=120)


class VerifySettings(_Section):
    # How long a move/focus/close may take to show up on the desktop before it counts as failed.
    timeout_s: float = Field(default=5.0, gt=0, le=60)


class AuditSettings(_Section):
    enabled: bool = True


class VoiceSettings(_Section):
    # Speech recognition: "large-v3-turbo" (best with accents and noise) or "small.en" (lighter).
    stt_model: str = Field(default="large-v3-turbo", pattern=r"^[A-Za-z0-9._-]{1,60}$")
    device: Literal["cuda", "cpu"] = "cuda"
    # SONU's voice (Kokoro): af_heart, af_bella, am_michael, bm_george, … — `sonu voice test` to hear one.
    voice: str = Field(default="af_heart", pattern=r"^[a-z]{2}_[a-z]{1,20}$")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # Say every reply out loud when you talk to SONU (typed requests stay silent).
    speak_replies: bool = True
    # Say "How can I help?" (or similar) when the SONU key is pressed, then listen.
    greet: bool = True
    # After answering, keep listening this many seconds for a follow-up (no key, no "Hey Sonu"); 0: off.
    follow_up_s: float = Field(default=6.0, ge=0, le=30)
    # Talk over SONU to stop it (adds a "SONU (echo cancel)" speaker and mic to PipeWire while sonud runs).
    barge_in: bool = True
    # Installing by voice: say the password as well as type it (off: only type it). SONU enters it into its
    # own install window and never shows, logs or keeps it — but anyone nearby hears it.
    password_by_voice: bool = False


class JournalSettings(_Section):
    # The activity journal (D6): which window had the focus and when, for "what was I doing yesterday?".
    # Local only; password managers and private browser windows are never recorded.
    enabled: bool = True
    keep_days: int = Field(default=30, ge=1, le=365)
    # More apps never to record (window classes or parts of them), e.g. ["signal", "thunderbird"].
    exclude_apps: list[str] = Field(default_factory=list, max_length=50)


class NotificationsSettings(_Section):
    # "What was that notification?" (D69): sonud keeps the latest notifications in memory only (never on
    # disk), shown and said only to you. Off unless turned on: notifications can carry message previews.
    remember: bool = False
    keep_minutes: int = Field(default=60, ge=1, le=24 * 60)
    # Apps never kept (names or parts of them), e.g. ["signal"]; password managers never are.
    exclude_apps: list[str] = Field(default_factory=list, max_length=50)


_DEFAULT_CLOUD_USES: tuple[CloudUse, ...] = ("prompts", "planning")


class CloudSettings(_Section):
    # Ollama Cloud models (D62), reached through the local Ollama once it's signed in (`ollama signin`).
    # Nothing is sent unless [privacy] cloud = "allow", and then only for the uses listed here.
    model: str = Field(default="gpt-oss:120b-cloud", min_length=1)  # prompts and planning
    vision_model: str = Field(default="gemma4:31b-cloud", min_length=1)
    # prompts : your idea for Claude Code (your words) → a better prompt
    # planning: harder requests, with window titles removed (only app names and workspaces go)
    # vision  : screenshots for "what's on my screen?" and clicking by looking — anything visible goes
    use_for: list[CloudUse] = Field(default_factory=lambda: list(_DEFAULT_CLOUD_USES), max_length=3)
    # hard: requests with several parts, `sonu --deep`, and ones the local model couldn't plan; always: all
    plan: Literal["hard", "always"] = "hard"
    think: bool = False


McpReach = Literal["local", "web", "account"]
SERVER_NAME = r"^[a-z][a-z0-9_]{0,23}$"


class McpServerSettings(_Section):
    """One outside MCP server (D63): SONU starts it (argv, no shell) and offers its tools to the planner."""

    command: list[str] = Field(min_length=1)  # e.g. ["uvx", "mcp-server-fetch"]; ~ is your home folder
    # What it can reach — SONU can't tell, so you say: local (stays on this laptop), web (sends what it's
    # given to the internet) or account (acts on an online account of yours).
    reaches: McpReach
    tools: list[str] = Field(default_factory=list)  # only these tools (empty: all, up to 32)
    # Tools you trust to only read. They run without asking — on web and account servers only when
    # every word they send is yours. Every other tool is high risk: asked each time, arguments shown.
    read_only: list[str] = Field(default_factory=list)
    # Environment for the server; it gets no other secrets of yours.
    env: dict[str, str] = Field(default_factory=dict)
    # Environment read from a command when the server starts, so a token stays out of this file:
    # { GITHUB_PERSONAL_ACCESS_TOKEN = ["gh", "auth", "token"] }. Never logged.
    env_commands: dict[str, list[str]] = Field(default_factory=dict)
    timeout_s: float = Field(default=60.0, gt=0, le=600)
    enabled: bool = True


class McpSettings(_Section):
    servers: dict[str, McpServerSettings] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _names(cls, servers: dict[str, McpServerSettings]) -> dict[str, McpServerSettings]:
        for name in servers:
            if not re.match(SERVER_NAME, name):
                raise ValueError(f"server name {name!r}: lowercase letters, digits and _ (up to 24)")
        return servers


class AssistantSettings(_Section):
    # The name SONU answers to and uses for itself in replies.
    name: str = Field(default="Sonu", min_length=1, max_length=30)
    # Your name, if you want SONU to use it now and then (e.g. "Martin").
    user_name: str | None = Field(default=None, max_length=40)
    # A short natural reply after every request (what SONU will say out loud in M3).
    replies: bool = True
    # The conversation ("switch it to …") ends after this many idle minutes.
    conversation_minutes: float = Field(default=10.0, ge=0, le=240)


class Settings(_Section):
    ollama: OllamaSettings = OllamaSettings()
    models: ModelsSettings = ModelsSettings()
    policy: PolicySettings = PolicySettings()
    privacy: PrivacySettings = PrivacySettings()
    hyprland: HyprlandSettings = HyprlandSettings()
    apps: AppsSettings = AppsSettings()
    web: WebSettings = WebSettings()
    verify: VerifySettings = VerifySettings()
    audit: AuditSettings = AuditSettings()
    assistant: AssistantSettings = AssistantSettings()
    voice: VoiceSettings = VoiceSettings()
    journal: JournalSettings = JournalSettings()
    notifications: NotificationsSettings = NotificationsSettings()
    cloud: CloudSettings = CloudSettings()
    mcp: McpSettings = McpSettings()


class ConfigError(Exception):
    """The config file exists but cannot be used."""


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    settings: Settings
    path: Path
    from_file: bool


def _format_validation_error(path: Path, error: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "(root)"
        lines.append(f"  - {location}: {issue['msg']}")
    return "\n".join(lines)


def load_settings(path: Path) -> LoadedSettings:
    """Load settings from ``path``; a missing file means "all defaults"."""
    if not path.exists():
        return LoadedSettings(settings=Settings(), path=path, from_file=False)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc
    return LoadedSettings(settings=settings, path=path, from_file=True)


DEFAULT_CONFIG_TEMPLATE = """\
# SONU configuration — every value below is the built-in default.
# Uncomment and change what you need. Unknown keys are rejected on purpose.

[ollama]
# host = "http://127.0.0.1:11434"
# timeout_s = 10.0
# chat_timeout_s = 120.0
# keep_alive = "10m"
# num_ctx = 16384

[models.planner]
# Understands requests and proposes plans. Must support tool calling.
# model = "qwen3.5:4b"
# think = false

[models.planner_deep]
# Used for harder requests. Bigger and slower.
# model = "qwen3.5:9b"
# think = true

[models.embed]
# Embeddings for semantic memory (later milestones).
# model = "qwen3-embedding:0.6b"

[policy]
# suggest       : only show plans, never act
# confirm-each  : ask before every action
# confirm-risky : act on low-risk steps, ask before medium/high-risk ones
# trusted       : ask only for high-risk steps (forbidden actions are always denied)
# autonomy = "confirm-risky"

[privacy]
# Whether any data may be sent to cloud models: never | allow ("ask" isn't built yet: it stays local)
# cloud = "never"

[hyprland]
# request_timeout_s = 2.0

[apps]
# Settle names that match several apps. Example (uncomment and edit):
#   aliases = { "vs code" = "codium.desktop" }
# aliases = {}
# launch_timeout_s = 20.0
# reuse_existing = true

[web]
# Where "search the web for …" goes: duckduckgo, google, brave or startpage.
# search_engine = "duckduckgo"
# load_timeout_s = 30.0

[verify]
# timeout_s = 5.0

[audit]
# Append-only, hash-chained log of everything SONU does.
# enabled = true

[assistant]
# name = "Sonu"
# Your name, used now and then — not set by default. Example: user_name = "Martin"
# replies = true              # a short natural reply after every request
# conversation_minutes = 10   # "it" / "that" refer back this long

[voice]
# Talking to SONU needs the voice extra and `sonu voice setup` (downloads the models).
# stt_model = "large-v3-turbo"   # or "small.en" (lighter)
# device = "cuda"
# voice = "af_heart"             # `sonu voice test` to hear the others
# speed = 1.0
# speak_replies = true
# greet = true                   # "How can I help?" when the key is pressed
# follow_up_s = 6.0              # keep listening after a reply for the next request (0: off)
# barge_in = true                # talk over SONU to stop it (a PipeWire echo canceller)
# password_by_voice = false      # installs by voice: say the password too (anyone nearby hears it)

[journal]
# Which window had the focus and when, kept on this computer only ("what was I doing yesterday?").
# Password managers and private browser windows are never recorded. `sonu journal pause` stops it.
# enabled = true
# keep_days = 30
# exclude_apps = []              # more apps never to record, e.g. ["signal"]

[notifications]
# "What was that notification?": sonud keeps the latest ones in memory only (never on disk) and shows
# or says them only to you. Off by default — notifications can carry message previews.
# remember = false
# keep_minutes = 60
# exclude_apps = []              # apps never kept, e.g. ["signal"]

[cloud]
# Ollama Cloud models, reached through your local Ollama after `ollama signin`. Nothing is sent unless
# [privacy] cloud = "allow" — then only for the uses below; every send is in the audit log (cloud.send).
# Cloud models never go in [models]: SONU refuses to start if one does.
# model = "gpt-oss:120b-cloud"          # prompts and planning
# vision_model = "gemma4:31b-cloud"
# prompts  : your idea for Claude Code (your own words) → a better prompt
# planning : harder requests, window titles removed (app names and workspaces still go)
# vision   : screenshots for "what's on my screen?" and clicking by looking — anything visible goes
# use_for = ["prompts", "planning"]
# plan = "hard"                         # hard (several parts, --deep, local failed) | always
# think = false

# Outside MCP servers (D63): SONU starts each one and can use its tools. Their tools are high risk —
# asked every time, with the arguments shown — except the ones you list as read_only. What they
# answer is shown to you and never given to a model (it could carry instructions).
# For example:
#   [mcp.servers.fetch]
#   command = ["uvx", "mcp-server-fetch"]
#   reaches = "web"                     # local | web | account — what it can reach
#   read_only = ["fetch"]               # runs without asking when every word it sends is yours
#   tools = []                          # only these (empty: all)
#   env = {}                            # environment for it (it gets no other secrets of yours)
#   env_commands = {}                   # e.g. { GITHUB_PERSONAL_ACCESS_TOKEN = ["gh", "auth", "token"] }
#   timeout_s = 60
"""
