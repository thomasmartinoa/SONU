"""A scripted chat model: answers planner requests from a table instead of running an LLM."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from sonu.llm.chat import ChatMessage, ChatModelError, ChatResult


class ScriptedChatModel:
    def __init__(
        self,
        replies: Mapping[str, dict[str, Any] | str] | Callable[[str], dict[str, Any] | str],
        name: str = "scripted",
    ) -> None:
        self.name = name
        self._replies = replies
        self.calls: list[list[ChatMessage]] = []

    async def complete_json(self, messages: list[ChatMessage], schema: dict[str, Any]) -> ChatResult:
        self.calls.append(list(messages))
        request = next(message.content for message in messages if message.role == "user")
        try:
            reply = self._replies(request) if callable(self._replies) else self._replies[request]
        except KeyError as exc:
            raise ChatModelError(f"no scripted reply for {request!r}") from exc
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return ChatResult(content=content, model=self.name, duration_s=0.01)
