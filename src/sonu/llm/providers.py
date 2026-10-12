"""Cloud brains from any provider (D94): Anthropic, Google Gemini, and anything that speaks the OpenAI chat
API (Groq, OpenRouter — including its free models —, a self-hosted OmniRoute, LM Studio, vLLM, …), beside
Ollama Cloud.

Each is a ``ChatModel`` + ``TextModel`` like the local one, so the planner, the reply writer, the page
reader, the prompt writer and the screen reader don't know which they talk to. They are only ever used
through ``CloudModel`` (``cloud.py``): every send is audited first, a usage limit pauses that provider, a
failure falls back to the local model — and only for the uses the user allowed (``[cloud] use_for``).

The API key is never in the config file: it comes from the system keyring (``secret-tool``, set with ``sonu
settings key <provider>``) or an environment variable the user names.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from sonu.llm.chat import ChatMessage, ChatModelError, ChatModelLimited, ChatResult

KEYRING_SERVICE = "sonu"
TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class Provider:
    name: str  # "anthropic", "groq", "my-omniroute"
    kind: str  # "anthropic" | "gemini" | "openai"
    base_url: str
    label: str  # how it's said: "Anthropic", "Groq"
    key: str = "keyring"  # "keyring", "env:NAME", or "none" (a local gateway with no key)


# Ready to use: add a key and pick a model. [providers.<name>] in the config adds more or changes these.
BUILT_IN: dict[str, Provider] = {
    "anthropic": Provider("anthropic", "anthropic", "https://api.anthropic.com", "Anthropic"),
    "gemini": Provider("gemini", "gemini", "https://generativelanguage.googleapis.com", "Google Gemini"),
    "groq": Provider("groq", "openai", "https://api.groq.com/openai/v1", "Groq"),
    "openrouter": Provider("openrouter", "openai", "https://openrouter.ai/api/v1", "OpenRouter"),
    "omniroute": Provider("omniroute", "openai", "http://127.0.0.1:20128/v1", "OmniRoute (on this laptop)"),
}


# What an optional field becomes when a service refuses it (absent: it's left out).
OPTIONAL_FALLBACK: dict[str, Any] = {"response_format": {"type": "json_object"}}


class KeyMissing(ChatModelError):
    pass


def lookup_key(
    provider: Provider, env: Mapping[str, str], run: Any = subprocess.run, which: Any = shutil.which
) -> str | None:
    """The provider's API key: from the keyring (``secret-tool``) or the environment variable it names."""
    if provider.key == "none":
        return ""
    if provider.key.startswith("env:"):
        return env.get(provider.key.removeprefix("env:")) or None
    if which("secret-tool", path=env.get("PATH")) is None:  # the environment's PATH: tests give none
        return None
    try:
        found = run(
            ["secret-tool", "lookup", "service", KEYRING_SERVICE, "provider", provider.name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if found.returncode != 0:
        return None
    return found.stdout.strip() or None


def store_key(name: str, key: str, run: Any = subprocess.run) -> bool:
    """Keep an API key in the system keyring (never in a file of SONU's)."""
    stored = run(
        ["secret-tool", "store", "--label", f"SONU · {name}", "service", KEYRING_SERVICE, "provider", name],
        input=key,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return bool(stored.returncode == 0)


def _image_media(data: str) -> str:
    return "image/jpeg" if data.startswith("/9j/") else "image/png"


class RemoteModel:
    """One model at one provider. ``transport``: tests pass a fake one."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
        think: bool = False,
    ) -> None:
        # think False: ask a reasoning model not to think (found on the laptop: qwen3.5 through an OpenAI-
        # style API thought for 8.7 s and spent the whole reply on it; reasoning_effort "none": 0.1 s)
        self.provider, self.name, self.key, self.think = provider, model, key, think
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_s)

    def check_key(self) -> None:
        """Before anything is recorded as sent: no key, nothing to send (found by a test — the audit log said
        "cloud.send" for a request that never left)."""
        if self.key is None:
            raise KeyMissing(
                f"no API key for {self.provider.label} — run `sonu settings key {self.provider.name}`"
            )

    # ---------------------------------------------------------------- the two ways SONU asks
    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        return await self._ask(messages, schema=schema, temperature=0.0, max_tokens=2048)

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        result = await self._ask(messages, schema=None, temperature=temperature, max_tokens=max_tokens)
        return ChatResult(
            result.content.strip(),
            result.model,
            result.duration_s,
            result.prompt_tokens,
            result.output_tokens,
        )

    async def _ask(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any] | None,
        temperature: float,
        max_tokens: int,
    ) -> ChatResult:
        if self.key is None:
            raise KeyMissing(
                f"no API key for {self.provider.label} — run `sonu settings key {self.provider.name}`"
            )
        started = time.monotonic()
        kind = self.provider.kind
        if kind == "anthropic":
            content, tokens = await self._anthropic(messages, schema, temperature, max_tokens)
        elif kind == "gemini":
            content, tokens = await self._gemini(messages, schema, temperature, max_tokens)
        else:
            content, tokens = await self._openai(messages, schema, temperature, max_tokens)
        return ChatResult(content, self.name, time.monotonic() - started, tokens[0], tokens[1])

    async def _post_without(
        self, url: str, body: dict[str, Any], headers: dict[str, str], optional: dict[str, tuple[str, ...]]
    ) -> dict[str, Any]:
        """Post; when the service refuses an optional field (named in its error), send it again without it."""
        for _ in range(len(optional) + 1):
            try:
                return await self._post(url, body, headers)
            except ChatModelLimited:
                raise
            except ChatModelError as exc:
                refused = [
                    field
                    for field, words in optional.items()
                    if field in body and any(word in str(exc) for word in words)
                ]
                if not refused:
                    raise
                for field in refused:
                    replacement = OPTIONAL_FALLBACK.get(field)
                    if replacement is not None and body.get(field) != replacement:
                        body[field] = replacement  # e.g. a JSON schema → plain JSON mode
                    else:
                        body.pop(field)
        return await self._post(url, body, headers)

    async def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ChatModelError(f"{self.provider.label} didn't answer ({exc.__class__.__name__})") from exc
        if response.status_code == 429:
            retry = response.headers.get("retry-after")
            seconds = float(retry) if retry and retry.replace(".", "", 1).isdigit() else None
            raise ChatModelLimited(f"{self.provider.label}: usage limit reached", seconds)
        if response.status_code in (401, 403):
            raise ChatModelError(
                f"{self.provider.label} refused the API key — "
                f"set it again: `sonu settings key {self.provider.name}`"
            )
        if response.status_code >= 400:
            raise ChatModelError(
                f"{self.provider.label} answered {response.status_code}: {response.text[:300]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ChatModelError(f"{self.provider.label} sent something that isn't JSON") from exc
        if not isinstance(data, dict):
            raise ChatModelError(f"{self.provider.label} sent an unexpected answer")
        return data

    # ---------------------------------------------------------------- OpenAI-compatible
    async def _openai(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None, temperature: float, max_tokens: int
    ) -> tuple[str, tuple[int, int]]:
        def part(message: ChatMessage) -> dict[str, Any]:
            if not message.images:
                return {"role": message.role, "content": message.content}
            pictures = [
                {"type": "image_url", "image_url": {"url": f"data:{_image_media(i)};base64,{i}"}}
                for i in message.images
            ]
            return {"role": message.role, "content": [{"type": "text", "text": message.content}, *pictures]}

        body: dict[str, Any] = {
            "model": self.name,
            "messages": [part(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema},
            }
        if not self.think:
            body["reasoning_effort"] = "none"
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        url = f"{self.provider.base_url.rstrip('/')}/chat/completions"
        # Not every OpenAI-compatible service takes a schema (then plain JSON mode: SONU checks the shape
        # itself) or a reasoning setting (then without it).
        optional = {"response_format": ("response_format", "json_schema"), "reasoning_effort": ("reasoning",)}
        data = await self._post_without(url, body, headers, optional)
        try:
            content = str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatModelError(f"{self.provider.label} sent no answer") from exc
        usage = data.get("usage") or {}
        return content, (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))

    # ---------------------------------------------------------------- Anthropic
    async def _anthropic(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None, temperature: float, max_tokens: int
    ) -> tuple[str, tuple[int, int]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")

        def part(message: ChatMessage) -> dict[str, Any]:
            pictures = [
                {"type": "image", "source": {"type": "base64", "media_type": _image_media(i), "data": i}}
                for i in message.images
            ]
            return {"role": message.role, "content": [*pictures, {"type": "text", "text": message.content}]}

        body: dict[str, Any] = {
            "model": self.name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [part(m) for m in messages if m.role != "system"],
        }
        if system:
            body["system"] = system
        if schema is not None:  # the answer as the input of one tool it must call: always valid JSON
            body["tools"] = [{"name": "answer", "description": "Give the answer.", "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "answer"}
        headers = {"x-api-key": str(self.key), "anthropic-version": "2023-06-01"}
        data = await self._post(f"{self.provider.base_url.rstrip('/')}/v1/messages", body, headers)
        blocks = data.get("content") or []
        if schema is not None:
            used = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"), None)
            if used is None:
                raise ChatModelError(f"{self.provider.label} didn't give a structured answer")
            content = json.dumps(used.get("input") or {})
        else:
            content = "".join(
                str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
        usage = data.get("usage") or {}
        return content, (int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))

    # ---------------------------------------------------------------- Google Gemini
    async def _gemini(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None, temperature: float, max_tokens: int
    ) -> tuple[str, tuple[int, int]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")

        def part(message: ChatMessage) -> dict[str, Any]:
            pictures = [{"inlineData": {"mimeType": _image_media(i), "data": i}} for i in message.images]
            role = "model" if message.role == "assistant" else "user"
            return {"role": role, "parts": [{"text": message.content}, *pictures]}

        config: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseJsonSchema"] = schema
        if not self.think:
            config["thinkingConfig"] = {"thinkingBudget": 0}
        body: dict[str, Any] = {
            "contents": [part(m) for m in messages if m.role != "system"],
            "generationConfig": config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self.provider.base_url.rstrip('/')}/v1beta/models/{self.name}:generateContent"
        headers = {"x-goog-api-key": str(self.key)}
        data: dict[str, Any] = {}
        for _ in range(3):  # a model that refuses a JSON schema or a thinking budget: asked again without it
            try:
                data = await self._post(url, body, headers)
                break
            except ChatModelLimited:
                raise
            except ChatModelError as exc:
                refused = [
                    f for f in ("responseJsonSchema", "thinkingConfig") if f in config and f in str(exc)
                ]
                if not refused:
                    raise
                for field in refused:
                    config.pop(field)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(str(p.get("text") or "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatModelError(f"{self.provider.label} sent no answer") from exc
        usage = data.get("usageMetadata") or {}
        return content, (int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0))

    async def aclose(self) -> None:
        await self._client.aclose()
