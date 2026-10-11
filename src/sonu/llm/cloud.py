"""Cloud models (M5 part 2, D62): Ollama Cloud, opt-in, and the guard on what leaves the laptop.

* Ollama Cloud models are reached through the local Ollama (``gpt-oss:120b-cloud``: the tag says it).
  Ollama sends them on to ollama.com — so a cloud model is decided by its name, never by the host.
* Nothing goes out unless ``[privacy] cloud = "allow"``, and then only for the uses in
  ``[cloud] use_for``: ``prompts`` (the user's idea for Claude Code), ``planning`` (window titles
  removed) and ``vision`` (screenshots). Every local role (``[models]``) must be a local model: SONU
  refuses to start otherwise, because those roles see window titles and the screen.
* Every send is in the audit log (``cloud.send``: what for, which model, how much, a fingerprint) —
  written *before* sending; if it can't be written, nothing is sent.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sonu.audit.log import AuditError, AuditLog
from sonu.config.settings import CloudUse, Settings
from sonu.llm.chat import ChatMessage, ChatModelError, ChatResult, OllamaChatModel

logger = logging.getLogger(__name__)


class CloudRefused(Exception):
    """The config would send something to a cloud model it wasn't allowed to."""


def is_cloud_model(name: str) -> bool:
    """``gpt-oss:120b-cloud``, ``gemma4:cloud`` — Ollama's naming for models it runs on ollama.com."""
    _, colon, tag = name.strip().lower().partition(":")
    return bool(colon) and (tag == "cloud" or tag.endswith("-cloud"))


def check_local_roles(settings: Settings) -> None:
    """The planner, reply writer, prompt writer and screen reader see window titles and the screen:
    they must run on this laptop. Cloud models belong in [cloud], used only for what it allows."""
    models = settings.models
    for role, model in (
        ("planner", models.planner.model),
        ("planner_deep", models.planner_deep.model),
        ("embed", models.embed.model),
    ):
        if is_cloud_model(model):
            raise CloudRefused(
                f"[models.{role}] is {model}, an Ollama Cloud model — everything it sees (window titles "
                'too) would leave the laptop. Put cloud models in [cloud] and set [privacy] cloud = "allow".'
            )


def cloud_allowed(settings: Settings, use: CloudUse) -> bool:
    return settings.privacy.cloud == "allow" and use in settings.cloud.use_for


def fingerprint(messages: list[ChatMessage]) -> tuple[int, int, str]:
    """Characters, pictures and a short SHA-256 of what is sent."""
    digest = hashlib.sha256()
    chars = images = 0
    for message in messages:
        digest.update(message.content.encode())
        chars += len(message.content)
        for image in message.images:
            digest.update(image.encode())
            images += 1
    return chars, images, digest.hexdigest()[:16]


class CloudModel:
    """A cloud model for one use: each send is audited first, and a failure says what to do."""

    def __init__(self, inner: OllamaChatModel, use: CloudUse, audit: AuditLog | None) -> None:
        self.inner, self.use, self.audit = inner, use, audit
        self.name = inner.name

    def _record(self, messages: list[ChatMessage]) -> None:
        chars, images, digest = fingerprint(messages)
        data = {"use": self.use, "model": self.name, "chars": chars, "images": images, "sha256": digest}
        if self.audit is None:
            logger.info("cloud.send %s", data)
            return
        try:
            self.audit.append("cloud.send", data)
        except (OSError, AuditError) as exc:
            raise ChatModelError(f"couldn't write the audit log, so nothing was sent ({exc})") from exc

    @staticmethod
    def _explain(exc: ChatModelError) -> ChatModelError:
        text = str(exc)
        if "unauthorized" in text.lower():
            return ChatModelError(f"{text} — Ollama isn't signed in to Ollama Cloud: run `ollama signin`")
        if "retired" in text.lower():
            return ChatModelError(f"{text} — pick another cloud model in [cloud] (see `sonu doctor`)")
        return exc

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        self._record(messages)
        try:
            return await self.inner.complete_json(messages, schema)
        except ChatModelError as exc:
            raise self._explain(exc) from exc

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        self._record(messages)
        try:
            return await self.inner.complete_text(messages, temperature=temperature, max_tokens=max_tokens)
        except ChatModelError as exc:
            raise self._explain(exc) from exc
