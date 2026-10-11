from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sonu.audit.log import AuditLog
from sonu.config.settings import (
    CloudSettings,
    ModelsSettings,
    OllamaSettings,
    PlannerRole,
    PrivacySettings,
    Settings,
)
from sonu.core.planner import LLMPlanner, RoutedPlanner, is_hard
from sonu.core.planning import PlanningError
from sonu.core.screen_reader import ScreenReader, ScreenReaderError
from sonu.desktop.hyprland import Monitor, Window
from sonu.llm import OllamaClient
from sonu.llm.chat import ChatMessage, ChatModelError, ChatModelLimited, OllamaChatModel
from sonu.llm.cloud import (
    CloudModel,
    CloudPause,
    CloudRefused,
    CloudUsage,
    LocalFallback,
    check_local_roles,
    cloud_allowed,
    cloud_usage,
    is_cloud_model,
)
from sonu.runtime import SetupError, model_parts
from sonu.testing.fake_hyprland import make_monitor, make_window
from sonu.testing.fake_ollama import FakeOllama
from sonu.tools.registry import ToolRegistry
from sonu.world import WorldSnapshot

pytestmark = pytest.mark.anyio

LOCAL, CLOUD, VISION = "qwen3.5:4b", "gpt-oss:120b-cloud", "gemma4:31b-cloud"
REGISTRY = ToolRegistry()
SNAPSHOT = WorldSnapshot(
    windows=(
        Window.model_validate(make_window("0xa1", "kitty", "ssh bank-server — zsh", workspace_id=1)),
        Window.model_validate(
            make_window("0xb2", "zen", "Diagnosis results — Patient portal", workspace_id=2)
        ),
    ),
    monitors=(Monitor.model_validate(make_monitor()),),
    active_address="0xa1",
)
PLAN = {
    "unsupported": [],
    "kind": "steps",
    "message": "",
    "steps": [{"tool": "apps.launch", "args": {"app": "kitty"}}],
}
HARD = "make a folder called demo in dev_works, open claude code there and tell it to add a readme"


def fake_ollama(*, signed_in: bool = True) -> FakeOllama:
    fake = FakeOllama()
    fake.add_model(LOCAL, size_gib=3.4, capabilities=["completion", "tools", "thinking", "vision"])
    fake.add_model(CLOUD, size_gib=60, capabilities=["completion", "tools", "thinking"])
    fake.add_model(VISION, size_gib=20, capabilities=["completion", "tools", "thinking", "vision"])
    fake.cloud_user = "martin" if signed_in else None
    fake.chat_responder = lambda body: json.dumps(PLAN) if body.get("format") else "A terminal."
    return fake


def allowed(*uses: str, plan: str = "hard") -> Settings:
    return Settings(
        privacy=PrivacySettings(cloud="allow"),
        cloud=CloudSettings(use_for=list(uses), plan=plan),  # type: ignore[arg-type]
    )


def system_prompts(fake: FakeOllama, model: str) -> list[str]:
    return [body["messages"][0]["content"] for body in fake.chat_requests if body["model"] == model]


# ---------------------------------------------------------------- what is a cloud model, what may go
def test_ollama_cloud_models_are_known_by_their_tag() -> None:
    assert is_cloud_model("gpt-oss:120b-cloud")
    assert is_cloud_model("gemma4:cloud")
    assert is_cloud_model("Kimi-K2.6:CLOUD")
    assert not is_cloud_model("qwen3.5:4b")
    assert not is_cloud_model("cloud")  # no tag
    assert not is_cloud_model("hf.co/someone/cloudy-model:Q4_K_M")


def test_a_cloud_model_in_models_is_refused() -> None:
    check_local_roles(Settings())
    for models in (
        ModelsSettings(planner=PlannerRole(model=CLOUD)),
        ModelsSettings.model_validate({"planner_deep": {"model": "kimi-k2.6:cloud"}}),
    ):
        with pytest.raises(CloudRefused, match="window titles too"):
            check_local_roles(Settings(models=models, privacy=PrivacySettings(cloud="allow")))


def test_nothing_goes_unless_allowed_and_listed() -> None:
    assert not cloud_allowed(Settings(), "prompts")  # never, by default
    assert not cloud_allowed(
        Settings(privacy=PrivacySettings(cloud="ask")), "prompts"
    )  # not built: stays local
    assert cloud_allowed(Settings(privacy=PrivacySettings(cloud="allow")), "planning")
    assert not cloud_allowed(
        Settings(privacy=PrivacySettings(cloud="allow")), "vision"
    )  # opt in to screenshots
    assert cloud_allowed(allowed("vision"), "vision")
    assert not cloud_allowed(allowed("vision"), "prompts")


def test_hard_requests() -> None:
    assert is_hard(HARD)
    assert is_hard("open kitty on 3, set the volume to 20 and turn on night light")
    assert not is_hard("open kitty on workspace 3")
    assert not is_hard("open kitty and zen")
    assert is_hard(" ".join(["word"] * 25))


# ---------------------------------------------------------------- every send audited first
async def test_each_send_is_audited_before_it_goes(tmp_path: Path) -> None:
    fake, audit = fake_ollama(), AuditLog(tmp_path / "audit.jsonl")
    client = OllamaClient(transport=fake.transport)
    model = CloudModel(OllamaChatModel(client, PlannerRole(model=CLOUD), OllamaSettings()), "prompts", audit)
    await model.complete_text([ChatMessage("user", "a todo app")], temperature=0.2, max_tokens=50)
    record, answer = list(audit.records())
    assert record.kind == "cloud.send"
    assert record.data["use"] == "prompts" and record.data["model"] == CLOUD
    assert record.data["chars"] == len("a todo app") and record.data["images"] == 0
    assert "todo" not in json.dumps(record.data)  # how much and a fingerprint, not the text
    assert answer.kind == "cloud.answer"  # and what came back, counted
    assert answer.data == {
        "use": "prompts",
        "model": CLOUD,
        "tokens_in": 900,
        "tokens_out": 40,
        "seconds": 0.25,
    }


async def test_no_audit_log_means_nothing_is_sent(tmp_path: Path) -> None:
    fake = fake_ollama()
    (tmp_path / "audit").mkdir()
    audit = AuditLog(tmp_path / "audit")  # a directory: can't be written
    client = OllamaClient(transport=fake.transport)
    model = CloudModel(OllamaChatModel(client, PlannerRole(model=CLOUD), OllamaSettings()), "prompts", audit)
    with pytest.raises(ChatModelError, match="nothing was sent"):
        await model.complete_text([ChatMessage("user", "x")], temperature=0, max_tokens=5)
    assert fake.chat_requests == []


async def test_not_signed_in_says_what_to_do(tmp_path: Path) -> None:
    fake = fake_ollama(signed_in=False)
    client = OllamaClient(transport=fake.transport)
    model = CloudModel(OllamaChatModel(client, PlannerRole(model=CLOUD), OllamaSettings()), "prompts", None)
    with pytest.raises(ChatModelError, match="run `ollama signin`"):
        await model.complete_text([ChatMessage("user", "x")], temperature=0, max_tokens=5)


# ---------------------------------------------------------------- planning
async def test_the_cloud_planner_never_sees_window_titles(tmp_path: Path) -> None:
    fake = fake_ollama()
    parts = model_parts(allowed("planning"), OllamaClient(transport=fake.transport), OllamaSettings(), None)
    plan = await parts.planner.plan(HARD, SNAPSHOT, REGISTRY)
    assert plan.model == CLOUD
    prompts = system_prompts(fake, CLOUD)
    assert prompts
    for prompt in prompts:
        assert "kitty" in prompt and "zen" in prompt  # app names and workspaces go
        assert "bank-server" not in prompt and "Patient portal" not in prompt
    assert "Diagnosis" not in json.dumps(fake.chat_requests)


async def test_easy_requests_stay_local_with_titles() -> None:
    fake = fake_ollama()
    parts = model_parts(allowed("planning"), OllamaClient(transport=fake.transport), OllamaSettings(), None)
    plan = await parts.planner.plan("open kitty on workspace 3", SNAPSHOT, REGISTRY)
    assert plan.model == LOCAL
    assert system_prompts(fake, CLOUD) == []
    assert "bank-server" in system_prompts(fake, LOCAL)[0]  # the local model may see them


async def test_always_sends_every_request_to_the_cloud() -> None:
    fake = fake_ollama()
    parts = model_parts(
        allowed("planning", plan="always"), OllamaClient(transport=fake.transport), OllamaSettings(), None
    )
    assert (await parts.planner.plan("open kitty", SNAPSHOT, REGISTRY)).model == CLOUD


async def test_cloud_away_plans_locally_and_local_failure_asks_the_cloud() -> None:
    fake = fake_ollama(signed_in=False)
    parts = model_parts(allowed("planning"), OllamaClient(transport=fake.transport), OllamaSettings(), None)
    assert (await parts.planner.plan(HARD, SNAPSHOT, REGISTRY)).model == LOCAL

    class Failing(LLMPlanner):
        async def plan(self, *args: Any, **kwargs: Any) -> Any:
            raise PlanningError("the planner model timed out")

    signed_in = fake_ollama()
    client = OllamaClient(transport=signed_in.transport)
    cloud = LLMPlanner(
        OllamaChatModel(client, PlannerRole(model=CLOUD), OllamaSettings()), include_titles=False
    )
    routed = RoutedPlanner(Failing(OllamaChatModel(client, PlannerRole(), OllamaSettings())), cloud)
    assert (await routed.plan("open kitty", SNAPSHOT, REGISTRY)).model == CLOUD
    both = RoutedPlanner(Failing(cloud.model), Failing(cloud.model))
    with pytest.raises(PlanningError, match="timed out"):
        await both.plan("open kitty", SNAPSHOT, REGISTRY)


async def test_deep_goes_to_the_cloud_when_planning_is_allowed() -> None:
    fake = fake_ollama()
    parts = model_parts(
        allowed("planning"), OllamaClient(transport=fake.transport), OllamaSettings(), None, deep=True
    )
    assert parts.planner_model == CLOUD
    assert (await parts.planner.plan("open kitty", SNAPSHOT, REGISTRY)).model == CLOUD


def test_a_cloud_model_by_hand_needs_planning_allowed() -> None:
    client = OllamaClient(transport=fake_ollama().transport)
    with pytest.raises(SetupError, match="is a cloud model"):
        model_parts(Settings(), client, OllamaSettings(), None, model=CLOUD)
    parts = model_parts(allowed("planning"), client, OllamaSettings(), None, model=CLOUD)
    assert isinstance(parts.planner, LLMPlanner) and parts.planner.include_titles is False
    with pytest.raises(SetupError, match="window titles too"):
        model_parts(
            Settings(models=ModelsSettings(planner=PlannerRole(model=CLOUD))), client, OllamaSettings(), None
        )


# ---------------------------------------------------------------- prompts and vision
def test_off_by_default_everything_is_local() -> None:
    parts = model_parts(Settings(), OllamaClient(transport=fake_ollama().transport), OllamaSettings(), None)
    assert parts.planner_model == LOCAL
    assert parts.writer.model.name == LOCAL and not isinstance(parts.writer.model, CloudModel)
    assert parts.reader.model.name == LOCAL and parts.reader.cloud is False


def test_each_use_is_its_own_choice() -> None:
    client = OllamaClient(transport=fake_ollama().transport)
    prompts = model_parts(allowed("prompts"), client, OllamaSettings(), None)
    writer = prompts.writer.model
    assert isinstance(writer, LocalFallback) and writer.cloud.use == "prompts" and writer.local.name == LOCAL
    assert prompts.planner_model == LOCAL and prompts.reader.model.name == LOCAL
    vision = model_parts(allowed("vision"), client, OllamaSettings(), None)
    assert vision.reader.model.name == VISION and vision.reader.cloud is True
    assert not isinstance(vision.writer.model, LocalFallback | CloudModel)


async def test_screenshots_go_to_the_cloud_only_when_vision_is_allowed(tmp_path: Path) -> None:
    fake, audit = fake_ollama(), AuditLog(tmp_path / "audit.jsonl")
    client = OllamaClient(transport=fake.transport)
    parts = model_parts(allowed("vision"), client, OllamaSettings(), audit)
    assert await parts.reader.read(b"png", "what's this?", "the screen") == "A terminal."
    record, _ = list(audit.records())
    assert record.data["use"] == "vision" and record.data["images"] == 1
    # A cloud model handed to the reader without that choice is refused.
    sneaky = ScreenReader(
        OllamaChatModel(client, PlannerRole(model=VISION), OllamaSettings()), "http://127.0.0.1:11434"
    )
    with pytest.raises(ScreenReaderError, match="is a cloud model"):
        await sneaky.read(b"png", "what's this?", "the screen")


async def test_a_cloud_answer_in_a_code_block_is_still_read(tmp_path: Path) -> None:
    # Found on the laptop (gemma4:31b-cloud, 2026-10-11): Ollama Cloud doesn't hold it to the JSON
    # schema — it pointed its own way ([{"point": [y, x]}]) or answered in a ```json block, and SONU
    # said "the model couldn't point at it".
    fake = fake_ollama()
    fake.chat_responder = lambda body: (
        '```json\n{"found": true, "x": 711, "y": 492}\n```'
        if '"found"' in body["messages"][-1]["content"]
        else '[{"point": [492, 711], "label": "blue circle"}]'  # its own way, unless the fields are named
    )
    client = OllamaClient(transport=fake.transport)
    parts = model_parts(allowed("vision"), client, OllamaSettings(), AuditLog(tmp_path / "audit.jsonl"))
    assert await parts.reader.locate(b"png", "the blue circle") == (0.711, 0.492)


# ---------------------------------------------------------------- usage limits
def cloud_chats(fake: FakeOllama) -> int:
    return sum(body["model"].endswith("cloud") for body in fake.chat_requests)


async def test_a_usage_limit_pauses_the_cloud_and_sonu_stays_local(tmp_path: Path) -> None:
    fake, audit = fake_ollama(), AuditLog(tmp_path / "audit.jsonl")
    fake.cloud_limited = True  # HTTP 429, no Retry-After
    client = OllamaClient(transport=fake.transport)
    parts = model_parts(allowed("planning", "prompts"), client, OllamaSettings(), audit, state_dir=tmp_path)
    assert (await parts.planner.plan(HARD, SNAPSHOT, REGISTRY)).model == LOCAL  # planned anyway
    assert cloud_chats(fake) == 1
    limited = [record.data for record in audit.records() if record.kind == "cloud.limited"]
    assert len(limited) == 1 and limited[0]["use"] == "planning"
    saved = json.loads((tmp_path / "cloud-pause.json").read_text())  # sonud, `sonu` and the doctor see it
    assert 14 * 60 < saved["until"] - time.time() <= 15 * 60
    # Paused: nothing more is sent, and nothing waits on the cloud.
    assert (await parts.planner.plan(HARD, SNAPSHOT, REGISTRY)).model == LOCAL
    idea = [ChatMessage("user", "a todo app")]
    reply = await parts.writer.model.complete_text(idea, temperature=0, max_tokens=9)
    assert reply.model == LOCAL  # the prompt is written locally instead
    assert cloud_chats(fake) == 1
    assert [record.kind for record in audit.records()].count("cloud.send") == 1


async def test_the_pause_lasts_as_long_as_ollama_says(tmp_path: Path) -> None:
    fake, now = fake_ollama(), [1000.0]
    fake.cloud_limited, fake.retry_after = True, 60
    pause = CloudPause(tmp_path / "pause.json", clock=lambda: now[0])
    client = OllamaClient(transport=fake.transport)
    inner = OllamaChatModel(client, PlannerRole(model=CLOUD), OllamaSettings())
    model = CloudModel(inner, "prompts", None, pause)
    with pytest.raises(ChatModelLimited, match=r"usage limit was reached .*hourly usage limit.* stays local"):
        await model.complete_text([ChatMessage("user", "x")], temperature=0, max_tokens=5)
    assert pause.until() == 1060.0
    now[0] = 1059.0
    with pytest.raises(ChatModelLimited, match="stays local until"):
        await model.complete_text([ChatMessage("user", "x")], temperature=0, max_tokens=5)
    assert cloud_chats(fake) == 1
    now[0], fake.cloud_limited = 1061.0, False
    assert (await model.complete_text([ChatMessage("user", "x")], temperature=0, max_tokens=5)).model == CLOUD


def test_use_is_counted_from_the_audit_log(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.append("cloud.send", {"use": "planning"})
    audit.append("cloud.answer", {"tokens_in": 900, "tokens_out": 40})
    audit.append("task.start", {"request": "x"})
    assert cloud_usage(audit, timedelta(days=1)) == CloudUsage(sends=1, tokens=940)
    later = datetime.now(UTC) + timedelta(days=2)
    assert cloud_usage(audit, timedelta(days=1), now=later) == CloudUsage()
    assert cloud_usage(audit, timedelta(days=7), now=later).sends == 1
