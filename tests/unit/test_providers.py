"""Cloud brains from any provider (D94): what each provider is sent, what SONU reads back, keys, limits."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import httpx
import pytest

from sonu.llm.chat import ChatMessage, ChatModelError, ChatModelLimited
from sonu.llm.providers import BUILT_IN, KeyMissing, Provider, RemoteModel, lookup_key

SCHEMA = {"type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"]}
ASK = [ChatMessage("system", "You plan."), ChatMessage("user", "open kitty")]


class Service:
    """A fake provider: records each request, answers with what the test gives."""

    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.answers.pop(0)

    def body(self, n: int = -1) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.seen[n].content)
        return result


def model(kind: str, service: Service, key: str | None = "secret", think: bool = False) -> RemoteModel:
    provider = BUILT_IN["groq"] if kind == "openai" else BUILT_IN[kind]
    return RemoteModel(provider, "some-model", key, transport=httpx.MockTransport(service), think=think)


@pytest.mark.anyio
async def test_openai_compatible_gets_a_schema_and_no_thinking() -> None:
    answer = {
        "choices": [{"message": {"content": '{"kind": "steps"}'}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3},
    }
    service = Service(httpx.Response(200, json=answer))
    result = await model("openai", service).complete_json(ASK, SCHEMA)
    assert result.content == '{"kind": "steps"}' and (result.prompt_tokens, result.output_tokens) == (9, 3)
    sent = service.body()
    assert service.seen[0].url == "https://api.groq.com/openai/v1/chat/completions"
    assert service.seen[0].headers["authorization"] == "Bearer secret"
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA
    assert sent["reasoning_effort"] == "none"  # found on the laptop: thinking spent the whole reply
    assert sent["messages"][0] == {"role": "system", "content": "You plan."}


@pytest.mark.anyio
async def test_a_service_that_refuses_an_option_is_asked_again_without_it() -> None:
    answer = {"choices": [{"message": {"content": '{"kind": "steps"}'}}]}
    service = Service(
        httpx.Response(400, text="response_format json_schema is not supported"),
        httpx.Response(400, text="unknown parameter: reasoning_effort"),
        httpx.Response(200, json=answer),
    )
    result = await model("openai", service).complete_json(ASK, SCHEMA)
    assert result.content == '{"kind": "steps"}'
    assert service.body(1)["response_format"] == {"type": "json_object"}  # plain JSON mode: SONU checks it
    assert "reasoning_effort" not in service.body(2)


@pytest.mark.anyio
async def test_anthropic_answers_through_a_tool_it_must_call() -> None:
    answer = {
        "content": [{"type": "tool_use", "name": "answer", "input": {"kind": "steps"}}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    service = Service(
        httpx.Response(200, json=answer),
        httpx.Response(200, json={"content": [{"type": "text", "text": "Hi!"}]}),
    )
    anthropic = model("anthropic", service)
    assert json.loads((await anthropic.complete_json(ASK, SCHEMA)).content) == {"kind": "steps"}
    sent = service.body(0)
    assert service.seen[0].headers["x-api-key"] == "secret" and sent["system"] == "You plan."
    assert (
        sent["tool_choice"] == {"type": "tool", "name": "answer"}
        and sent["tools"][0]["input_schema"] == SCHEMA
    )
    assert all(m["role"] != "system" for m in sent["messages"])
    picture = [ChatMessage("user", "what's this?", images=("iVBORw0KGgo=",))]
    assert (await anthropic.complete_text(picture, temperature=0.4, max_tokens=50)).content == "Hi!"
    assert service.body(1)["messages"][0]["content"][0]["type"] == "image"


@pytest.mark.anyio
async def test_gemini_gets_a_schema_and_no_thinking_and_falls_back() -> None:
    answer = {"candidates": [{"content": {"parts": [{"text": '{"kind": "steps"}'}]}}]}
    service = Service(
        httpx.Response(400, text="Unknown name thinkingConfig"), httpx.Response(200, json=answer)
    )
    result = await model("gemini", service).complete_json(ASK, SCHEMA)
    assert result.content == '{"kind": "steps"}'
    first, second = service.body(0), service.body(1)
    assert first["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert first["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert (
        "thinkingConfig" not in second["generationConfig"]
        and first["systemInstruction"]["parts"][0]["text"] == "You plan."
    )
    assert service.seen[0].headers["x-goog-api-key"] == "secret"


@pytest.mark.anyio
async def test_gemini_refusing_a_big_schema_without_naming_it() -> None:
    """Found on real hardware (gemini-3.8-flash): SONU's plan schema was refused with only "Request contains
    an invalid argument"; asked again without it, the plan came back right — and later requests skip it."""
    answer = {"candidates": [{"content": {"parts": [{"text": '{"kind": "steps"}'}]}}]}
    refused = '{"error": {"code": 400, "message": "Request contains an invalid argument."}}'
    service = Service(
        httpx.Response(400, text=refused), httpx.Response(200, json=answer), httpx.Response(200, json=answer)
    )
    gemini = model("gemini", service)
    assert (await gemini.complete_json(ASK, SCHEMA)).content == '{"kind": "steps"}'
    assert "responseJsonSchema" in service.body(0)["generationConfig"]
    assert "responseJsonSchema" not in service.body(1)["generationConfig"]
    assert service.body(1)["generationConfig"]["responseMimeType"] == "application/json"
    await gemini.complete_json(ASK, SCHEMA)
    assert "responseJsonSchema" not in service.body(2)["generationConfig"]  # not tried again


@pytest.mark.anyio
async def test_limits_refused_keys_and_missing_keys_are_said_plainly() -> None:
    limited = Service(httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(ChatModelLimited) as caught:
        await model("openai", limited).complete_text(ASK, temperature=0, max_tokens=10)
    assert caught.value.retry_after_s == 30
    with pytest.raises(ChatModelError, match="sonu settings key anthropic"):
        await model("anthropic", Service(httpx.Response(401))).complete_text(
            ASK, temperature=0, max_tokens=10
        )
    with pytest.raises(KeyMissing, match="sonu settings key gemini"):
        await model("gemini", Service(), key=None).complete_text(ASK, temperature=0, max_tokens=10)


def test_keys_come_from_the_keyring_or_a_named_variable() -> None:
    calls: list[list[str]] = []

    def keyring(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="sk-from-keyring\n")

    found = lambda name, path=None: "/usr/bin/secret-tool"  # noqa: E731 — the program, without running it
    assert lookup_key(BUILT_IN["anthropic"], {}, run=keyring, which=found) == "sk-from-keyring"
    assert calls[0][:2] == ["secret-tool", "lookup"] and calls[0][-1] == "anthropic"
    named = Provider("mine", "openai", "https://example.com/v1", "Mine", "env:MY_KEY")
    assert lookup_key(named, {"MY_KEY": "sk-env"}) == "sk-env"
    assert lookup_key(named, {}) is None
    local = Provider("omni", "openai", "http://127.0.0.1:20128/v1", "OmniRoute", "none")
    assert lookup_key(local, {}) == ""  # a local gateway without a key


def test_without_a_keyring_the_key_is_kept_in_a_private_file(tmp_path: Any) -> None:
    """Found on the owner's laptop: no Secret Service runs, so `secret-tool store` fails."""
    import stat as stat_module

    from sonu.llm.providers import store_key

    def no_keyring(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stderr="The name is not activatable")

    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}
    where = store_key("sarvam", "sk-sarvam", env, run=no_keyring)
    assert where is not None and "readable only by you" in where
    path = tmp_path / "data" / "sonu" / "keys" / "sarvam"
    assert stat_module.S_IMODE(path.stat().st_mode) == 0o600
    assert stat_module.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert lookup_key(BUILT_IN["sarvam"], env, run=no_keyring) == "sk-sarvam"
    assert lookup_key(BUILT_IN["sarvam"], {"PATH": ""}) is None  # no HOME (tests): no file looked at


def test_the_control_center_sees_a_saved_key_and_where_it_is(tmp_path: Any) -> None:
    """Found on real hardware: a key pasted in the Control Center looked lost after a restart (the box was
    empty again). `sonu settings providers --json` says it's saved — its last 4 characters, never more — and
    where."""
    import json

    from typer.testing import CliRunner

    from sonu.cli.app import app

    keys = tmp_path / "data" / "sonu" / "keys"
    keys.mkdir(parents=True)
    (keys / "gemini").write_text("AIzaSyExampleKey1234zaDA\n")
    (tmp_path / "run").mkdir()
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "PATH": "",  # no secret-tool: the file
    }
    result = CliRunner().invoke(app, ["settings", "providers", "--json"], env=env)
    assert result.exit_code == 0, result.output
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["gemini"]["key"] and rows["gemini"]["ends"] == "zaDA"
    assert rows["gemini"]["where"].startswith("a private file")
    assert "AIzaSy" not in result.stdout  # never the key itself
    assert rows["anthropic"] == {**rows["anthropic"], "key": False, "ends": "", "where": ""}
