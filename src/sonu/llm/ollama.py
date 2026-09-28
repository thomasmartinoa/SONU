"""Minimal Ollama client.

Milestone 0 only needs health and inventory queries (``/api/version``, ``/api/tags``,
``/api/show``, ``/api/ps``). Chat, structured outputs and tool calling arrive with the planner
in milestone 1, behind the provider-neutral ``ChatModel`` protocol.
"""

from __future__ import annotations

from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class OllamaError(Exception):
    """Ollama is unreachable or answered unexpectedly."""


class OllamaNotFound(OllamaError):
    """The endpoint or model does not exist (HTTP 404)."""


class _OllamaModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ModelDetails(_OllamaModel):
    family: str = ""
    parameter_size: str = ""
    quantization_level: str = ""


class InstalledModel(_OllamaModel):
    name: str
    size: int = 0  # bytes on disk
    digest: str = ""
    details: ModelDetails = ModelDetails()


class LoadedModel(_OllamaModel):
    name: str
    size: int = 0  # bytes in memory
    size_vram: int = 0  # bytes of that in GPU memory
    expires_at: str = ""


class ModelInfo(_OllamaModel):
    capabilities: tuple[str, ...] = ()
    details: ModelDetails = ModelDetails()

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities


class ChatStats(_OllamaModel):
    total_duration: int = 0  # nanoseconds
    load_duration: int = 0
    prompt_eval_count: int = 0
    eval_count: int = 0


class ChatReply(_OllamaModel):
    content: str
    thinking: str = ""
    stats: ChatStats = ChatStats()


class _Tags(_OllamaModel):
    models: list[InstalledModel] = Field(default_factory=list)


class _Running(_OllamaModel):
    models: list[LoadedModel] = Field(default_factory=list)


class OllamaClient:
    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.host,
            timeout=timeout_s,
            transport=transport,
            # Ollama is local by default; never pick up proxy settings from the environment for it.
            trust_env=False,
            headers={"User-Agent": "sonu"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code == 404:
            raise OllamaNotFound(f"{path}: not found (404) — {response.text[:200]}")
        if response.is_error:
            raise OllamaError(f"{path}: HTTP {response.status_code} — {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise OllamaError(f"{path}: reply is not JSON") from exc

    async def version(self) -> str:
        data = await self._request("GET", "/api/version")
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str):
            raise OllamaError("/api/version: unexpected reply")
        return version

    async def installed_models(self) -> list[InstalledModel]:
        data = await self._request("GET", "/api/tags")
        try:
            return _Tags.model_validate(data).models
        except ValidationError as exc:
            raise OllamaError(f"/api/tags: unexpected reply: {exc.errors()[0]['msg']}") from exc

    async def loaded_models(self) -> list[LoadedModel]:
        data = await self._request("GET", "/api/ps")
        try:
            return _Running.model_validate(data).models
        except ValidationError as exc:
            raise OllamaError(f"/api/ps: unexpected reply: {exc.errors()[0]['msg']}") from exc

    async def load(self, model: str, *, keep_alive: str, timeout_s: float = 120.0) -> None:
        """Load a model into memory (and keep it for ``keep_alive``), or unload it with "0".

        Ollama does this on ``/api/generate`` with no prompt.
        """
        try:
            response = await self._client.post(
                "/api/generate", json={"model": model, "keep_alive": keep_alive}, timeout=timeout_s
            )
        except httpx.HTTPError as exc:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {exc.__class__.__name__}: {exc}") from exc
        if response.is_error:
            raise OllamaError(f"/api/generate: HTTP {response.status_code} — {response.text[:200]}")

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        format_schema: dict[str, Any] | None = None,
        think: bool | None = None,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
        timeout_s: float | None = None,
    ) -> ChatReply:
        """One non-streaming chat completion. ``format_schema`` constrains the reply to a JSON schema."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if format_schema is not None:
            body["format"] = format_schema
        if think is not None:
            body["think"] = think
        if options:
            body["options"] = options
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        try:
            response = await self._client.post(
                "/api/chat",
                json=body,
                timeout=timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as exc:
            raise OllamaError(f"{model} took too long to answer") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code == 404:
            raise OllamaNotFound(f"model '{model}' is not installed — run `ollama pull {model}`")
        if response.is_error:
            raise OllamaError(f"/api/chat: HTTP {response.status_code} — {response.text[:300]}")
        try:
            data = response.json()
            message = data.get("message") or {}
            return ChatReply(
                content=message.get("content", ""),
                thinking=message.get("thinking", "") or "",
                stats=ChatStats.model_validate(data),
            )
        except (ValueError, ValidationError, AttributeError) as exc:
            raise OllamaError("/api/chat: unexpected reply") from exc

    async def model_info(self, name: str) -> ModelInfo:
        try:
            data = await self._request("POST", "/api/show", json={"model": name})
        except OllamaNotFound as exc:
            raise OllamaNotFound(f"model '{name}' is not installed — run `ollama pull {name}`") from exc
        try:
            return ModelInfo.model_validate(data)
        except ValidationError as exc:
            raise OllamaError(f"/api/show: unexpected reply: {exc.errors()[0]['msg']}") from exc


def same_model(a: str, b: str) -> bool:
    """Compare Ollama model names, treating a missing tag as ``:latest``."""

    def canonical(name: str) -> str:
        name = name.strip().lower()
        last = name.rsplit("/", 1)[-1]
        return name if ":" in last else f"{name}:latest"

    return canonical(a) == canonical(b)
