"""The provider-neutral chat interface the planner depends on, and its Ollama implementation.

Other providers (OpenAI-compatible, Anthropic, Gemini) will implement the same ``ChatModel``
protocol in milestone 5 without any change to the planner or the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sonu.config.settings import ModelRole, OllamaSettings
from sonu.llm.ollama import OllamaClient, OllamaError, OllamaLimited

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str
    images: tuple[str, ...] = ()  # base64 pictures (a screenshot for the vision model)

    def as_dict(self) -> dict[str, Any]:
        if self.images:
            return {"role": self.role, "content": self.content, "images": list(self.images)}
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    model: str
    duration_s: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0


class ChatModelError(Exception):
    """The model could not be reached or did not answer."""


class ChatModelLimited(ChatModelError):
    """A usage limit was reached (Ollama Cloud, HTTP 429)."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def _chat_error(exc: OllamaError) -> ChatModelError:
    if isinstance(exc, OllamaLimited):
        return ChatModelLimited(str(exc), exc.retry_after_s)
    return ChatModelError(str(exc))


class ChatModel(Protocol):
    name: str

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult: ...


class TextModel(Protocol):
    name: str

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult: ...


class OllamaChatModel:
    """A chat model served by Ollama: JSON plans (schema-constrained) or short free text (replies)."""

    def __init__(self, client: OllamaClient, role: ModelRole, settings: OllamaSettings) -> None:
        self.client = client
        self.role = role
        self.settings = settings
        self.name = role.model
        self._supports_thinking: bool | None = None

    async def _thinking_supported(self) -> bool:
        if self._supports_thinking is None:
            try:
                info = await self.client.model_info(self.name)
            except OllamaError as exc:
                raise ChatModelError(str(exc)) from exc
            self._supports_thinking = "thinking" in info.capabilities
        return self._supports_thinking

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        # Thinking models (e.g. Qwen3.5) think by default, which is slow for short plans: turn it off
        # unless the role asks for it. Non-thinking models must not receive the parameter at all.
        think = self.role.think if await self._thinking_supported() else None
        try:
            reply = await self.client.chat(
                self.name,
                [message.as_dict() for message in messages],
                format_schema=schema,
                think=think,
                options={"temperature": 0, "num_ctx": self.settings.num_ctx},
                keep_alive=self.settings.keep_alive,
                timeout_s=self.settings.chat_timeout_s,
            )
        except OllamaError as exc:
            raise _chat_error(exc) from exc
        return ChatResult(
            content=reply.content,
            model=self.name,
            duration_s=reply.stats.total_duration / 1e9,
            prompt_tokens=reply.stats.prompt_eval_count,
            output_tokens=reply.stats.eval_count,
        )

    async def complete_text(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> ChatResult:
        """A short free-text reply (what SONU says back); thinking stays off — it's a sentence or two."""
        think = False if await self._thinking_supported() else None
        try:
            reply = await self.client.chat(
                self.name,
                [message.as_dict() for message in messages],
                think=think,
                options={
                    "temperature": temperature,
                    "num_ctx": self.settings.num_ctx,
                    "num_predict": max_tokens,
                },
                keep_alive=self.settings.keep_alive,
                timeout_s=self.settings.chat_timeout_s,
            )
        except OllamaError as exc:
            raise _chat_error(exc) from exc
        return ChatResult(
            content=reply.content.strip(),
            model=self.name,
            duration_s=reply.stats.total_duration / 1e9,
            prompt_tokens=reply.stats.prompt_eval_count,
            output_tokens=reply.stats.eval_count,
        )
