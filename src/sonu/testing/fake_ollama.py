"""An in-memory fake of the parts of Ollama's HTTP API that SONU uses, as an httpx transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

GIB = 1024**3


class FakeOllama:
    def __init__(self, version: str = "0.34.3") -> None:
        self.version = version
        self.models: dict[str, dict[str, Any]] = {}
        self.loaded: list[dict[str, Any]] = []
        self.requests: list[str] = []
        # /api/chat: gets the request body, returns the assistant's content (e.g. plan JSON).
        self.chat_responder: Callable[[dict[str, Any]], str] | None = None
        self.chat_requests: list[dict[str, Any]] = []
        self.keep_alive: dict[str, str] = {}  # /api/generate load/unload requests: model → keep_alive
        # Ollama Cloud: who this Ollama is signed in as (None: not signed in → 401 for cloud models),
        # and cloud models that were retired (410, as the real one answers).
        self.cloud_user: str | None = None
        self.retired: set[str] = set()
        # A usage limit reached: cloud chats answer 429 (with this Retry-After, if any).
        self.cloud_limited = False
        self.retry_after: int | None = None

    def add_model(self, name: str, *, size_gib: float, capabilities: list[str]) -> None:
        self.models[name] = {"size": int(size_gib * GIB), "capabilities": capabilities}

    def load(self, name: str, *, vram_gib: float) -> None:
        size = self.models[name]["size"]
        self.loaded.append({"name": name, "model": name, "size": size, "size_vram": int(vram_gib * GIB)})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if request.method == "GET" and path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if request.method == "GET" and path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": name, "model": name, "size": spec["size"], "digest": "0" * 64, "details": {}}
                        for name, spec in self.models.items()
                    ]
                },
            )
        if request.method == "GET" and path == "/api/ps":
            return httpx.Response(200, json={"models": self.loaded})
        if request.method == "POST" and path == "/api/generate":
            body = json.loads(request.content)
            if body.get("model") not in self.models:
                return httpx.Response(404, json={"error": f"model '{body.get('model')}' not found"})
            self.keep_alive[body["model"]] = str(body.get("keep_alive"))
            return httpx.Response(200, json={"model": body["model"], "response": "", "done": True})
        if request.method == "POST" and path == "/api/me":
            if self.cloud_user is None:
                return httpx.Response(
                    401, json={"error": "unauthorized", "signin_url": "https://ollama.com/connect"}
                )
            return httpx.Response(200, json={"name": self.cloud_user})
        if request.method == "POST" and path in ("/api/chat", "/api/show"):
            name = json.loads(request.content).get("model", "")
            if name in self.retired:
                return httpx.Response(410, json={"error": f"{name} was retired"})
        if request.method == "POST" and path == "/api/chat":
            body = json.loads(request.content)
            self.chat_requests.append(body)
            if str(body.get("model", "")).endswith("cloud") and self.cloud_user is None:
                return httpx.Response(401, json={"error": "Unauthorized"})
            if str(body.get("model", "")).endswith("cloud") and self.cloud_limited:
                headers = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
                error = {"error": "you've reached your hourly usage limit"}
                return httpx.Response(429, json=error, headers=headers)
            if body.get("model") not in self.models:
                return httpx.Response(404, json={"error": f"model '{body.get('model')}' not found"})
            content = self.chat_responder(body) if self.chat_responder else "{}"
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                    "total_duration": 250_000_000,
                    "prompt_eval_count": 900,
                    "eval_count": 40,
                },
            )
        if request.method == "POST" and path == "/api/show":
            name = json.loads(request.content).get("model", "")
            spec = self.models.get(name)
            if spec is None:
                return httpx.Response(404, json={"error": f"model '{name}' not found"})
            return httpx.Response(200, json={"capabilities": spec["capabilities"], "details": {}})
        return httpx.Response(404, json={"error": "not found"})
