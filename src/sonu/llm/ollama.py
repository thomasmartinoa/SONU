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
            raise OllamaError(f"{path}: not found (404) — {response.text[:200]}")
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

    async def model_info(self, name: str) -> ModelInfo:
        data = await self._request("POST", "/api/show", json={"model": name})
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
