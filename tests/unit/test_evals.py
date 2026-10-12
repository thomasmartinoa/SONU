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
    "partly-unsupported": {
        **steps(("apps.launch", {"app": "bluetooth manager"})),
        "unsupported": ["pair my new headphones"],
    },
    "website-by-name": steps(("web.open", {"site": "youtube"})),
    "site-search": steps(("web.search", {"site": "youtube", "query": "networkchuck"})),
    "browser-then-search": steps(
        ("apps.launch", {"app": "chromium", "workspace": 3}),
        ("web.search", {"query": "hyprland lua config", "browser": "chromium"}),
    ),
    "link-typed-by-user": steps(("web.open", {"site": "github.com/hyprwm/Hyprland"})),
    "search-injection-in-title": steps(("windows.move", {"window": {"app": "browser"}, "workspace": 3})),
    "bluetooth-on": steps(("settings.bluetooth", {"on": True})),
    "volume-and-mic": steps(
        ("settings.volume", {"target": "speaker", "level": 30}),
        ("settings.volume", {"target": "mic", "mute": True}),
    ),
    "volume-down-a-bit": steps(("settings.volume", {"target": "speaker", "change": -10})),
    "brightness-level": steps(("settings.brightness", {"level": 70})),
    "pause-media": steps(("media.control", {"action": "pause"})),
    "power-saver": steps(("settings.power_mode", {"mode": "power-saver"})),
    "wifi-off-asks": steps(("settings.wifi", {"on": False})),
    "battery-status": steps(("system.status", {"what": ["battery"]})),
    "next-song-with-player-open": steps(("media.control", {"action": "next"})),
    "play-a-specific-song": steps(("media.play", {"query": "faded"})),
    "song-on-youtube": steps(("media.play", {"query": "faded"})),
    "reminder-in-minutes": steps(("reminders.set", {"text": "check the oven", "in_minutes": 15})),
    "timer": steps(("reminders.set", {"text": "timer", "in_minutes": 5})),
    "screenshot-window": steps(("screen.capture", {"what": "window"})),
    "night-light-and-dnd": steps(
        ("desktop.shell", {"action": "night_light_on"}), ("desktop.shell", {"action": "do_not_disturb_on"})
    ),
    "keep-awake": steps(("desktop.shell", {"action": "keep_awake_on"})),
    "laptop-health": steps(("system.status", {"what": ["cpu", "memory", "temperature"]})),
    "wifi-connect-saved": steps(("settings.wifi_connect", {"network": "hithere"})),
    "wifi-connect-new": steps(("settings.wifi_connect", {"network": "cafe"})),
    "headphones-connect": steps(("settings.bluetooth_device", {"device": "headphones"})),
    "dnd-off-keeps-notifications": steps(
        ("desktop.shell", {"action": "night_light_off"}), ("desktop.shell", {"action": "do_not_disturb_off"})
    ),
    "how-is-my-laptop": steps(("system.status", {"what": ["health"]})),
    "play-with-a-title-on-screen": steps(("media.play", {"query": "faded", "browser": "chromium"})),
    "fullscreen-this-window": steps(("windows.state", {"window": {"focused": True}, "state": "fullscreen"})),
    "maximize-this-window": steps(("windows.state", {"window": {"focused": True}, "state": "maximized"})),
    "exit-fullscreen": steps(("windows.state", {"window": {"focused": True}, "state": "normal"})),
    "float-a-window": steps(("windows.state", {"window": {"app": "kitty"}, "state": "floating"})),
    "pin-on-every-workspace": steps(("windows.state", {"window": {"app": "browser"}, "state": "pinned"})),
    "side-by-side": steps(("windows.side_by_side", {"first": {"app": "zen"}, "second": {"app": "kitty"}})),
    "two-terminals-side-by-side": steps(
        ("windows.side_by_side", {"first": {"app": "terminal"}, "second": {"app": "terminal"}})
    ),
    "open-downloads": steps(("files.open", {"place": "downloads"})),
    "open-a-project-folder": steps(("files.open", {"place": "SONU"})),
    "open-last-download": steps(("files.open", {"place": "downloads", "newest": True})),
    "whats-playing": steps(("system.status", {"what": ["media"]})),
    "list-reminders": steps(("reminders.list", {})),
    "cancel-a-reminder": steps(("reminders.cancel", {"about": "oven"})),
    "theme-by-name": steps(("desktop.theme", {"action": "set", "name": "tokyo night"})),
    "light-mode": steps(("desktop.theme", {"action": "light"})),
    "which-theme": steps(("system.status", {"what": ["theme"]})),
    "next-wallpaper": steps(("desktop.wallpaper", {"action": "next"})),
    "read-clipboard": steps(("clipboard.read", {})),
    "suspend-asks": steps(("system.power", {"action": "suspend"})),
    "mute-then-shut-down": steps(
        ("settings.volume", {"target": "speaker", "mute": True}), ("system.power", {"action": "shutdown"})
    ),
    "record-the-screen": steps(("obs.record", {"action": "start"})),
    "stop-recording": steps(("obs.record", {"action": "stop"})),
    "obs-scene": steps(("obs.scene", {"scene": "only screen"})),
    "obs-mute-mic": steps(("obs.mute", {"source": "mic", "mute": True})),
    "go-live-asks": steps(("obs.control", {"action": "stream_start"})),
    "is-obs-recording": steps(("system.status", {"what": ["obs"]})),
    "play-on-apple-music": {
        "kind": "cannot",
        "message": "I can't play songs on Apple Music by name yet.",
        "steps": [],
        "unsupported": ["play faded on apple music"],
    },
    "play-on-spotify": steps(("media.play", {"query": "believer by imagine dragons", "service": "spotify"})),
    "remember-app-alias": steps(("memory.remember", {"kind": "alias", "text": "vs code", "app": "code oss"})),
    "remember-my-name": steps(("memory.remember", {"kind": "name", "text": "Martin"})),
    "remember-a-note": steps(
        ("memory.remember", {"kind": "note", "text": "the shop project is in dev_works/shop-app"})
    ),
    "what-do-you-remember": steps(("memory.list", {})),
    "wake-word-on": steps(("voice.wake", {"on": True})),
    "wake-word-off": steps(("voice.wake", {"on": False})),
    "wake-word-misheard": steps(("voice.wake", {"on": True})),
    "folder-and-claude-code": steps(
        ("files.make_folder", {"name": "shop-app", "inside": "dev_works"}), ("dev.claude_open", {})
    ),
    "claude-code-is-not-the-code-app": steps(
        ("files.make_folder", {"name": "sonu-m4-test", "inside": "dev_works"}),
        ("dev.claude_open", {"workspace": 9}),
    ),
    "whats-on-my-screen": steps(("screen.read", {"question": "what's on my screen right now?"})),
    "read-the-error-in-this-window": steps(
        ("screen.read", {"question": "read the error in this window", "window": {"focused": True}})
    ),
    "read-a-window-on-another-workspace": steps(
        ("screen.read", {"question": "what is open in zen?", "window": {"app": "zen"}})
    ),
    "press-a-button-sonu-doesnt-know": steps(
        ("screen.press", {"control": "frobnicate", "window": {"app": "kitty"}})
    ),
    "type-here-no-claude": steps(("screen.type", {"text": "Hello", "window": {"focused": True}})),
    "open-terminal-and-type": steps(
        ("apps.launch", {"app": "terminal", "workspace": 3}), ("screen.type", {"text": "hello"})
    ),
    "type-into-this-window": steps(
        ("screen.type", {"text": "hello world", "window": {"focused": True}, "enter": True})
    ),
    "install-an-app": steps(("packages.install", {"name": "vlc"})),
    "uninstall-an-app": steps(("packages.remove", {"name": "vlc"})),
    "get-rid-of-an-app": steps(("packages.remove", {"name": "obs studio"})),
    "what-was-i-doing-yesterday": steps(("journal.read", {"when": "yesterday"})),
    "the-last-hour-from-the-journal": steps(("journal.read", {"when": "last_hour"})),
    "when-did-i-last-use": steps(("journal.read", {"about": "btop"})),
    "pause-the-journal": steps(("journal.recording", {"on": False})),
    "resume-song": steps(("media.control", {"action": "play"})),
    "night-light-off-and-dnd": steps(
        ("desktop.shell", {"action": "night_light_off"}), ("desktop.shell", {"action": "do_not_disturb_off"})
    ),
    "github-user-search": steps(("web.search", {"query": "username thomasmartinoa", "site": "github"})),
    "open-project-in-editor": steps(("dev.open_project", {"folder": "SONU", "editor": "vscodium"})),
    "open-file-at-a-line": steps(("dev.open_project", {"folder": "SONU", "file": "planner.py", "line": 40})),
    "run-project-tests": steps(("dev.run", {"task": "tests", "folder": "SONU"})),
    "what-did-i-change": steps(("dev.git", {"folder": "SONU"})),
    "last-commits": steps(("dev.git", {"folder": "SONU", "what": "commits"})),
    "terminal-in-a-folder": steps(("dev.terminal", {"folder": "dev_works"})),
    "save-a-layout": steps(("layouts.save", {"name": "work"})),
    "restore-a-layout": steps(("layouts.restore", {"name": "work"})),
    "list-layouts": steps(("layouts.list", {})),
    "unread-messages": steps(("messages.unread", {})),
    "unread-in-one-app": steps(("messages.unread", {"app": "telegram"})),
    "what-was-that-notification": steps(("notifications.read", {})),
    "read-my-notifications": steps(("notifications.read", {"which": "recent"})),
    "switch-to-a-tab": steps(("browser.tab", {"words": "youtube"})),
    "close-a-tab": steps(("browser.close_tabs", {"words": "youtube"})),
    "list-tabs": steps(("browser.tabs", {})),
    "summarise-this-page": steps(("browser.read", {"question": "summarise this page"})),
    "click-a-link-on-the-page": steps(("browser.click", {"control": "pricing"})),
    "search-on-the-page": steps(
        ("browser.fill", {"text": "red shoes", "field": "search box", "submit": True})
    ),
    "scroll-the-page": steps(("browser.scroll", {"how": "down"})),
    "find-a-page-in-history": steps(("browser.history", {"words": "lentil soup", "when": "yesterday"})),
    "open-a-bookmark": steps(("browser.open_saved", {"words": "arch wiki"})),
    "open-a-file-by-name": steps(("files.open", {"place": "kestrel lake photo"})),
    "find-a-file": steps(("files.find", {"name": "invoice"})),
    "move-a-file": steps(("files.move", {"name": "invoice", "to": "downloads"})),
    "rename-a-file": steps(("files.rename", {"name": "kestrel lake photo", "new_name": "lake view"})),
    "delete-the-last-download": steps(("files.trash", {"name": "my last download"})),
    "copy-a-files-path": steps(("clipboard.copy", {"what": "path", "name": "invoice"})),
    "copy-the-last-screenshot": steps(("clipboard.copy", {"what": "screenshot"})),
    "play-my-music": steps(("media.play_local", {})),
    "shuffle-an-artist": steps(("media.play_local", {"words": "kestrel band"})),
    "why-is-my-fan-loud": steps(("system.processes", {})),
    "did-anything-crash": steps(("system.problems", {})),
    "kill-a-program": steps(("system.stop_program", {"program": "chromium"})),
    "summarise-a-document": steps(("files.read", {"name": "invoice pdf"})),
    "ask-a-document": steps(("files.read", {"name": "trip notes", "question": "what about parking?"})),
    "brief-me": steps(("system.briefing", {})),
    "find-something-out": steps(("web.research", {"query": "how kestrels hunt"})),
    "what-did-we-talk-about": steps(("conversations.recall", {"when": "yesterday"})),
    "what-did-you-tell-me": steps(("conversations.recall", {"about": "kestrels"})),
    "corrected-mid-sentence": steps(("media.play", {"query": "faded"})),
    "open-sonu-settings": steps(("control.open", {})),
    "ask-about-a-tab": steps(
        ("browser.read", {"question": "what does the github tab say about installing?", "words": "github"})
    ),
    "folder-and-a-terminal-there": steps(
        ("files.make_folder", {"name": "demo", "inside": "dev_works"}), ("dev.terminal", {"workspace": 4})
    ),
    "install-in-the-users-words": steps(("packages.install", {"name": "obs studio"})),
    "install-already-there": steps(("packages.install", {"name": "kitty"})),
    "github-open-prs": steps(("github.prs", {})),
    "github-merge-asks": steps(("github.merge", {"repo": "SONU"})),
    "github-failing-checks-not-merged": steps(("github.merge", {"repo": "hypr dotfiles"})),
    "claude-code-prompt-from-an-idea": steps(
        ("dev.claude_open", {"folder": "SONU"}), ("dev.prompt", {"idea": "a todo app with a dark mode"})
    ),
    "claude-code-in-a-folder-asks-to-trust": steps(("dev.claude_open", {"folder": "SONU", "workspace": 3})),
    "claude-code-and-a-prompt": steps(
        ("dev.claude_open", {"folder": "SONU"}),
        ("dev.type", {"text": "add a dark mode toggle to the settings page"}),
    ),
    "claude-code-on-sonnet": steps(
        ("dev.claude_open", {"folder": "SONU"}), ("dev.claude_model", {"model": "sonnet"})
    ),
    "complex-apps-search-volume": steps(
        ("apps.launch", {"app": "kitty", "workspace": 3}),
        ("apps.launch", {"app": "chromium", "workspace": 4}),
        ("web.search", {"query": "lofi hip hop", "site": "youtube", "browser": "chromium"}),
        ("settings.volume", {"target": "speaker", "level": 30}),
    ),
    "complex-evening": steps(
        ("desktop.shell", {"action": "night_light_on"}),
        ("settings.brightness", {"level": 40}),
        ("media.control", {"action": "pause"}),
        ("reminders.set", {"text": "stretch", "in_minutes": 20}),
    ),
    "complex-side-by-side-dnd-dark": steps(
        ("windows.side_by_side", {"first": {"app": "kitty"}, "second": {"app": "zen"}}),
        ("desktop.shell", {"action": "do_not_disturb_on"}),
        ("desktop.theme", {"action": "dark"}),
    ),
    "complex-with-an-unsupported-part": {
        **steps(("apps.launch", {"app": "kitty", "workspace": 2}), ("settings.bluetooth", {"on": True})),
        "unsupported": ["order me a pizza"],
    },
    "complex-three-questions": steps(
        ("desktop.describe", {}), ("system.status", {"what": ["cpu", "memory", "temperature", "battery"]})
    ),
    "complex-move-show-maximize": steps(
        ("windows.move", {"window": {"app": "kitty"}, "workspace": 5}),
        ("workspaces.show", {"workspace": 5}),
        ("windows.state", {"window": {"app": "kitty"}, "state": "maximized"}),
    ),
    "complex-dev-setup": steps(
        ("apps.launch", {"app": "code oss", "workspace": 2}),
        ("apps.launch", {"app": "chromium", "workspace": 3}),
        ("web.open", {"site": "github.com/thomasmartinoa/SONU", "browser": "chromium"}),
        ("workspaces.show", {"workspace": 2}),
    ),
    "complex-spoken-rambling": steps(
        ("apps.launch", {"app": "kitty", "workspace": 4}),
        ("apps.launch", {"app": "browser", "workspace": 4}),
        ("media.play", {"query": "alone by marshmello"}),
    ),
    "complex-alone-music": steps(
        ("apps.launch", {"app": "kitty", "workspace": 4}),
        ("apps.launch", {"app": "browser", "workspace": 4}),
        ("media.play", {"query": "alone music"}),
    ),
    # The browser is left out on purpose: SONU uses the one the request names.
    "play-in-the-named-browser": steps(("media.play", {"query": "lofi"})),
    "song-in-a-browser-without-media-controls": steps(
        ("media.play", {"query": "lofi beats", "browser": "chromium"})
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

    # The words' case doesn't matter: the model may keep the capitals of a name (seen in a real-model run).
    site = cases["site-search"]
    capitals = steps(("web.search", {"query": "NetworkChuck", "site": "youtube"}))
    result = await run_case(site, ScriptedChatModel({site.request: capitals}), tmp_path / "c")
    assert result.passed, result.failures


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


def test_eval_cloud_scores_the_cloud_model_without_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg_env: dict[str, str]
) -> None:
    import json

    from typer.testing import CliRunner

    from sonu.cli.app import app
    from sonu.llm import OllamaClient
    from sonu.testing.fake_ollama import FakeOllama

    fake = FakeOllama()
    fake.add_model("gpt-oss:120b-cloud", size_gib=60, capabilities=["completion", "tools", "thinking"])
    fake.cloud_user = "martin"
    fake.chat_responder = lambda body: json.dumps(GOLDEN["move-by-title"])

    def client(host: str, timeout_s: float) -> OllamaClient:
        return OllamaClient(host, timeout_s=timeout_s, transport=fake.transport)

    monkeypatch.setattr("sonu.cli.eval_cmd.OllamaClient", client)
    result = CliRunner().invoke(app, ["eval", "--cloud", "--json", "-c", "move-by-title"], env=xdg_env)
    assert result.exit_code == 0, result.output
    [summary] = json.loads(result.stdout)
    assert summary["model"] == "gpt-oss:120b-cloud"
    assert summary["passed"] == 1  # "the GitHub window": the user's words are enough
    prompt = fake.chat_requests[0]["messages"][0]["content"]
    assert "GitHub - SONU - Zen Browser" not in prompt and "YouTube - Zen Browser" not in prompt
