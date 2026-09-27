"""SONU's configuration: a single TOML file validated by pydantic.

Unknown keys are rejected (``extra="forbid"``) so a typo like ``autonmy = "trusted"``
fails loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

AutonomyMode = Literal["suggest", "confirm-each", "confirm-risky", "trusted"]
CloudPolicy = Literal["never", "ask", "allow"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OllamaSettings(_Section):
    host: str = "http://127.0.0.1:11434"
    timeout_s: float = Field(default=10.0, gt=0, le=600)
    # Generation can include loading the model into GPU memory on first use.
    chat_timeout_s: float = Field(default=120.0, gt=0, le=1800)
    keep_alive: str = "10m"
    num_ctx: int = Field(default=8192, ge=2048, le=262144)


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
# num_ctx = 8192

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
# Whether any data may be sent to cloud models: never | ask | allow
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
"""
