"""Cloud models (M5 part 2, D62): Ollama Cloud, opt-in, and the guard on what leaves the laptop.

* Ollama Cloud models are reached through the local Ollama (``gpt-oss:120b-cloud``: the tag says it).
  Ollama sends them on to ollama.com — so a cloud model is decided by its name, never by the host.
* Nothing goes out unless ``[privacy] cloud = "allow"``, and then only for the uses in
  ``[cloud] use_for``: ``prompts`` (the user's idea for Claude Code), ``planning`` (window titles
  removed) and ``vision`` (screenshots). Every local role (``[models]``) must be a local model: SONU
  refuses to start otherwise, because those roles see window titles and the screen.
* Every send is in the audit log (``cloud.send``: what for, which model, how much, a fingerprint) —
  written *before* sending; if it can't be written, nothing is sent. What came back is counted too
  (``cloud.answer``: tokens, seconds), so `sonu doctor` can show the day's and the week's use.
* A usage limit (HTTP 429) pauses the cloud — for as long as Ollama says, else 15 minutes — and SONU
  stays local meanwhile: nothing is sent, nothing waits on it (``cloud.limited``; a file in SONU's
  state folder, so sonud, `sonu` and `sonu doctor` all know). Prompts and vision fall back to the
  local model too, like planning: a request never fails because of the cloud.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sonu.audit.log import AuditError, AuditLog
from sonu.config.settings import CloudUse, Settings
from sonu.llm.chat import ChatMessage, ChatModelError, ChatModelLimited, ChatResult, OllamaChatModel
from sonu.llm.providers import RemoteModel

logger = logging.getLogger(__name__)


# Found on the laptop: gemma4:31b-cloud, asked for JSON, answers in a ```json block — Ollama Cloud
# doesn't hold every model to the format.
_FENCED = re.compile(r"\A\s*```[a-z]*\s*\n(.*?)\n?\s*```\s*\Z", re.DOTALL)


def unfenced(text: str) -> str:
    match = _FENCED.match(text)
    return match.group(1) if match else text


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


DEFAULT_PAUSE_S = 15 * 60  # a 429 without Retry-After: try again after this
PAUSE_FILE = "cloud-pause.json"  # in SONU's state folder


@dataclass
class CloudPause:
    """Until when the cloud is paused after a usage limit (a small JSON file; None: in memory only)."""

    path: Path | None = None
    clock: Callable[[], float] = time.time
    _until: float = 0.0

    def until(self) -> float | None:
        until = self._until
        if self.path is not None:
            with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
                until = max(until, float(json.loads(self.path.read_text())["until"]))
        return until if until > self.clock() else None

    def start(self, seconds: float, why: str) -> float:
        self._until = self.clock() + seconds
        if self.path is not None:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"until": self._until, "why": why[:200]}))
            except OSError as exc:
                logger.warning("couldn't save the cloud pause: %s", exc)
        return self._until


def staying_local(until: float) -> str:
    return f"SONU stays local until {datetime.fromtimestamp(until).strftime('%H:%M')}"


class CloudModel:
    """A cloud model for one use: each send is audited first, and a failure says what to do."""

    def __init__(
        self,
        inner: OllamaChatModel | RemoteModel,
        use: CloudUse,
        audit: AuditLog | None,
        pause: CloudPause | None = None,
        *,
        label: str = "Ollama Cloud",  # who it is, in what SONU says (D94: any provider)
    ) -> None:
        self.inner, self.use, self.audit, self.label = inner, use, audit, label
        self.pause = pause or CloudPause()
        self.name = inner.name

    def _audit(self, kind: str, data: dict[str, Any]) -> None:
        if self.audit is None:
            logger.info("%s %s", kind, data)
            return
        with contextlib.suppress(OSError, AuditError):
            self.audit.append(kind, data)

    def _check_pause(self) -> None:
        until = self.pause.until()
        if until is not None:
            raise ChatModelLimited(f"{self.label}'s usage limit was reached — {staying_local(until)}")

    def _done(self, result: ChatResult) -> None:
        self._audit(
            "cloud.answer",
            {
                "use": self.use,
                "model": self.name,
                "tokens_in": result.prompt_tokens,
                "tokens_out": result.output_tokens,
                "seconds": round(result.duration_s, 2),
            },
        )

    def _limited(self, exc: ChatModelLimited) -> ChatModelLimited:
        until = self.pause.start(exc.retry_after_s or DEFAULT_PAUSE_S, str(exc))
        until_text = datetime.fromtimestamp(until).isoformat(timespec="minutes")
        self._audit("cloud.limited", {"use": self.use, "model": self.name, "until": until_text})
        return ChatModelLimited(f"{self.label}'s usage limit was reached ({exc}) — {staying_local(until)}")

    def _record(self, messages: list[ChatMessage]) -> None:
        chars, images, digest = fingerprint(messages)
        data = {
            "use": self.use,
            "provider": self.label,
            "model": self.name,
            "chars": chars,
            "images": images,
            "sha256": digest,
        }
        if self.audit is None:
            logger.info("cloud.send %s", data)
            return
        try:
            self.audit.append("cloud.send", data)
        except (OSError, AuditError) as exc:
            raise ChatModelError(f"couldn't write the audit log, so nothing was sent ({exc})") from exc

    def _explain(self, exc: ChatModelError) -> ChatModelError:
        text = str(exc)
        if self.label != "Ollama Cloud":
            return exc  # a provider's own message already says what to do
        if "unauthorized" in text.lower():
            return ChatModelError(f"{text} — Ollama isn't signed in to Ollama Cloud: run `ollama signin`")
        if "retired" in text.lower():
            return ChatModelError(f"{text} — pick another cloud model in [cloud] (see `sonu doctor`)")
        return exc

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        self._check_pause()
        if isinstance(self.inner, RemoteModel):
            self.inner.check_key()
        self._record(messages)
        try:
            result = await self.inner.complete_json(messages, schema)
        except ChatModelLimited as exc:
            raise self._limited(exc) from exc
        except ChatModelError as exc:
            raise self._explain(exc) from exc
        self._done(result)
        return replace(result, content=unfenced(result.content))

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        self._check_pause()
        if isinstance(self.inner, RemoteModel):
            self.inner.check_key()
        self._record(messages)
        try:
            result = await self.inner.complete_text(messages, temperature=temperature, max_tokens=max_tokens)
        except ChatModelLimited as exc:
            raise self._limited(exc) from exc
        except ChatModelError as exc:
            raise self._explain(exc) from exc
        self._done(result)
        return result


class LocalFallback:
    """The cloud model, or the local one when the cloud can't answer (not signed in, offline, a usage
    limit): prompts and vision never fail because of the cloud (D62)."""

    def __init__(self, cloud: CloudModel, local: OllamaChatModel) -> None:
        self.cloud, self.local = cloud, local
        self.name = cloud.name

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        try:
            return await self.cloud.complete_json(messages, schema)
        except ChatModelError as exc:
            logger.info("%s: %s — using %s", self.cloud.use, exc, self.local.name)
        return await self.local.complete_json(messages, schema)

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        try:
            return await self.cloud.complete_text(messages, temperature=temperature, max_tokens=max_tokens)
        except ChatModelError as exc:
            logger.info("%s: %s — using %s", self.cloud.use, exc, self.local.name)
        return await self.local.complete_text(messages, temperature=temperature, max_tokens=max_tokens)


@dataclass(frozen=True, slots=True)
class CloudUsage:
    sends: int = 0
    tokens: int = 0  # in and out, as Ollama counted them


def cloud_usage(audit: AuditLog, since: timedelta, now: datetime | None = None) -> CloudUsage:
    """What SONU sent to the cloud in the last ``since`` (from the audit log)."""
    start = (now or datetime.now(UTC)) - since
    sends = tokens = 0
    for record in audit.records():
        if record.kind not in ("cloud.send", "cloud.answer"):
            continue
        try:
            when = datetime.fromisoformat(record.ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < start:
            continue
        if record.kind == "cloud.send":
            sends += 1
        else:
            tokens += int(record.data.get("tokens_in", 0)) + int(record.data.get("tokens_out", 0))
    return CloudUsage(sends, tokens)
