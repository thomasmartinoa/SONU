from __future__ import annotations

import json

import httpx
import pytest

from sonu.llm import OllamaClient, OllamaError, same_model
from sonu.testing.fake_ollama import FakeOllama


@pytest.mark.anyio
async def test_inventory_queries() -> None:
    fake = FakeOllama(version="0.34.3")
    fake.add_model("qwen3:8b", size_gib=5.2, capabilities=["completion", "tools", "thinking"])
    fake.add_model("qwen3-embedding:0.6b", size_gib=0.6, capabilities=["embedding"])
    fake.load("qwen3:8b", vram_gib=5.2)
    async with OllamaClient(transport=fake.transport) as client:
        assert await client.version() == "0.34.3"
        installed = await client.installed_models()
        assert [model.name for model in installed] == ["qwen3:8b", "qwen3-embedding:0.6b"]
        info = await client.model_info("qwen3:8b")
        assert info.supports_tools
        assert not (await client.model_info("qwen3-embedding:0.6b")).supports_tools
        loaded = await client.loaded_models()
        assert loaded[0].name == "qwen3:8b"
        assert loaded[0].size_vram > 5 * 1024**3


@pytest.mark.anyio
async def test_unknown_model_is_an_error() -> None:
    async with OllamaClient(transport=FakeOllama().transport) as client:
        with pytest.raises(OllamaError, match="404"):
            await client.model_info("nope:1b")


@pytest.mark.anyio
async def test_unreachable_server() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with OllamaClient(transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(OllamaError, match="cannot reach Ollama"):
            await client.version()


@pytest.mark.anyio
async def test_server_errors_and_bad_replies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=b"not json")

    async with OllamaClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OllamaError, match="HTTP 500"):
            await client.version()
        with pytest.raises(OllamaError, match="not JSON"):
            await client.installed_models()


@pytest.mark.anyio
async def test_show_sends_model_name() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"capabilities": ["completion"]})

    async with OllamaClient(transport=httpx.MockTransport(handler)) as client:
        await client.model_info("llama3.1:8b")
    assert seen == [{"model": "llama3.1:8b"}]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("qwen3.5:4b", "qwen3.5:4b", True),
        ("nomic-embed-text", "nomic-embed-text:latest", True),
        ("Qwen3:8B", "qwen3:8b", True),
        ("qwen3:8b", "qwen3:14b", False),
        ("hf.co/org/model:Q4_K_M", "hf.co/org/model:Q4_K_M", True),
        ("hf.co/org/model", "hf.co/org/model:latest", True),
    ],
)
def test_same_model(a: str, b: str, expected: bool) -> None:
    assert same_model(a, b) is expected
