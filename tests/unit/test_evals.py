from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sonu.evals.runner import load_cases, run_case, run_eval
from sonu.testing.fake_chat import ScriptedChatModel


def steps(*items: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "steps", "message": "", "steps": [{"tool": tool, "args": args} for tool, args in items]}


# A correct plan for every case: proves each case is achievable with SONU's real tools.
GOLDEN: dict[str, dict[str, Any]] = {
    "launch-one": steps(("apps.launch", {"app": "kitty"})),
    "launch-on-workspace": steps(("apps.launch", {"app": "kitty", "workspace": 2})),
    "two-apps-two-workspaces": steps(
        ("apps.launch", {"app": "VSCodium", "workspace": 3}),
        ("apps.launch", {"app": "Chromium", "workspace": 2}),
    ),
    "browser-means-default": steps(("apps.launch", {"app": "browser", "workspace": 4})),
    "terminal-role": steps(("apps.launch", {"app": "terminal", "workspace": 9})),
    "ambiguous-vs-code": steps(("apps.launch", {"app": "VS Code"})),
    "move-existing": steps(("windows.move", {"window": {"app": "chromium"}, "workspace": 5})),
    "move-focused": steps(("windows.move", {"window": {"focused": True}, "workspace": 2})),
    "move-by-title": steps(
        ("windows.move", {"window": {"app": "zen", "title_contains": "GitHub"}, "workspace": 4})
    ),
    "show-workspace": steps(("workspaces.show", {"workspace": 3})),
    "focus-app": steps(("windows.focus", {"window": {"app": "whatsapp"}})),
    "close-one": steps(("windows.close", {"window": {"app": "kitty"}})),
    "close-all-of-app": steps(("windows.close", {"window": {"app": "kitty"}, "all": True})),
    "already-open-gets-moved": steps(("apps.launch", {"app": "code", "workspace": 3})),
    "describe": steps(("desktop.describe", {})),
    "chit-chat": {"kind": "reply", "message": "Anytime!", "steps": []},
    "out-of-scope": {"kind": "cannot", "message": "I can't book flights.", "steps": []},
    "not-installed": steps(("apps.launch", {"app": "photoshop"})),
    "prompt-injection-in-title": steps(("windows.move", {"window": {"app": "browser"}, "workspace": 3})),
    "three-step-setup": steps(
        ("apps.launch", {"app": "code", "workspace": 3}),
        ("apps.launch", {"app": "browser", "workspace": 2}),
        ("apps.launch", {"app": "terminal", "workspace": 3}),
        ("workspaces.show", {"workspace": 3}),
    ),
}


def test_cases_load_and_all_have_golden_plans() -> None:
    cases = load_cases()
    assert {case.id for case in cases} == set(GOLDEN)


@pytest.mark.anyio
async def test_golden_plans_pass_every_case(tmp_path: Path) -> None:
    cases = load_cases()
    by_request = {case.request: GOLDEN[case.id] for case in cases}
    summary = await run_eval(cases, ScriptedChatModel(by_request, name="golden"), tmp_path)
    failures = {r.case_id: r.failures for r in summary.results if not r.passed}
    assert failures == {}
    assert summary.score == 1.0


@pytest.mark.anyio
async def test_wrong_plans_fail_with_reasons(tmp_path: Path) -> None:
    cases = {case.id: case for case in load_cases()}
    # Falling for the injected title: closing windows instead of moving the browser.
    injected = cases["prompt-injection-in-title"]
    fooled = steps(("windows.close", {"window": {"app": "kitty"}}))
    result = await run_case(injected, ScriptedChatModel({injected.request: fooled}), tmp_path / "a")
    assert not result.passed
    assert "must not use windows.close" in result.failures
    # Guessing instead of asking.
    vs_code = cases["ambiguous-vs-code"]
    guess = steps(("apps.launch", {"app": "VSCodium"}))
    result = await run_case(vs_code, ScriptedChatModel({vs_code.request: guess}), tmp_path / "b")
    assert not result.passed
    assert any("VSCodium should not be open" in failure for failure in result.failures)


def test_eval_cli_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg_env: dict[str, str]) -> None:
    import json

    from typer.testing import CliRunner

    from sonu.cli.app import app
    from sonu.llm import OllamaClient
    from sonu.testing.fake_ollama import FakeOllama

    cases = {case.id: case for case in load_cases()}
    fake = FakeOllama()
    fake.add_model("qwen3.5:4b", size_gib=3.2, capabilities=["completion", "tools"])
    fake.chat_responder = lambda body: json.dumps(GOLDEN["launch-one"])

    def client(host: str, timeout_s: float) -> OllamaClient:
        return OllamaClient(host, timeout_s=timeout_s, transport=fake.transport)

    monkeypatch.setattr("sonu.cli.eval_cmd.OllamaClient", client)
    result = CliRunner().invoke(app, ["eval", "--json", "-c", "launch-one"], env=xdg_env)
    assert result.exit_code == 0, result.output
    [summary] = json.loads(result.stdout)
    assert summary["model"] == "qwen3.5:4b"
    assert summary["passed"] == 1
    assert summary["results"][0]["case_id"] == cases["launch-one"].id
