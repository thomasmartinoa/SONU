from __future__ import annotations

import json
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
from sonu.llm.chat import ChatMessage, ChatModelError, OllamaChatModel
from sonu.llm.cloud import CloudModel, CloudRefused, check_local_roles, cloud_allowed, is_cloud_model
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
    [record] = list(audit.records())
    assert record.kind == "cloud.send"
    assert record.data["use"] == "prompts" and record.data["model"] == CLOUD
    assert record.data["chars"] == len("a todo app") and record.data["images"] == 0
    assert "todo" not in json.dumps(record.data)  # how much and a fingerprint, not the text


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
    assert isinstance(prompts.writer.model, CloudModel) and prompts.writer.model.use == "prompts"
    assert prompts.planner_model == LOCAL and prompts.reader.model.name == LOCAL
    vision = model_parts(allowed("vision"), client, OllamaSettings(), None)
    assert vision.reader.model.name == VISION and vision.reader.cloud is True
    assert not isinstance(vision.writer.model, CloudModel)


async def test_screenshots_go_to_the_cloud_only_when_vision_is_allowed(tmp_path: Path) -> None:
    fake, audit = fake_ollama(), AuditLog(tmp_path / "audit.jsonl")
    client = OllamaClient(transport=fake.transport)
    parts = model_parts(allowed("vision"), client, OllamaSettings(), audit)
    assert await parts.reader.read(b"png", "what's this?", "the screen") == "A terminal."
    [record] = list(audit.records())
    assert record.data["use"] == "vision" and record.data["images"] == 1
    # A cloud model handed to the reader without that choice is refused.
    sneaky = ScreenReader(
        OllamaChatModel(client, PlannerRole(model=VISION), OllamaSettings()), "http://127.0.0.1:11434"
    )
    with pytest.raises(ScreenReaderError, match="is a cloud model"):
        await sneaky.read(b"png", "what's this?", "the screen")
