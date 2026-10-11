from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from sonu.config import Settings, SonuPaths
from sonu.config.settings import ModelsSettings, PlannerRole
from sonu.doctor import CheckResult, CommandResult, DoctorContext, run_checks
from sonu.doctor.checks import check_system
from sonu.integrations.hypr_theme import DefaultApp, detect_hypr_theme
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from sonu.testing.fake_ollama import FakeOllama

NVIDIA_SMI = "NVIDIA GeForce RTX 3070 Ti Laptop GPU, 8192, 512, 615.71.09\n"
SECRET_TITLE = "Inbox (3) - private@example.com - Zen"
UNCENSORED = "hf.co/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive:Q4_K_M"


def authors_ollama() -> FakeOllama:
    """Roughly the model set from the author's machine."""
    fake = FakeOllama(version="0.34.3")
    fake.add_model("qwen3:8b", size_gib=4.8, capabilities=["completion", "tools", "thinking"])
    fake.add_model("llama3.1:8b", size_gib=4.6, capabilities=["completion", "tools"])
    fake.add_model(
        "gemma4:e4b", size_gib=8.9, capabilities=["completion", "tools", "thinking", "vision", "audio"]
    )
    fake.add_model(
        "qwen3.6:35b-a3b", size_gib=20.5, capabilities=["completion", "tools", "thinking", "vision"]
    )
    # Seen on the author's machine: this embedding model also reports "tools".
    fake.add_model("qwen3-embedding:0.6b", size_gib=0.6, capabilities=["embedding", "tools"])
    fake.add_model("nomic-embed-text:latest", size_gib=0.3, capabilities=["embedding"])
    fake.add_model(UNCENSORED, size_gib=5.9, capabilities=["completion", "tools", "vision"])
    return fake


def make_context(
    xdg_env: dict[str, str],
    *,
    hypr: FakeHyprland | None,
    ollama: FakeOllama | None,
    settings: Settings | None = None,
    tools: Sequence[str] = ("nvidia-smi", "notify-send", "systemd-run", "gio", "uwsm", "grim", "qs"),
) -> DoctorContext:
    env = dict(xdg_env) | {"WAYLAND_DISPLAY": "wayland-1", "XDG_CURRENT_DESKTOP": "Hyprland"}
    env |= hypr.env if hypr else {"XDG_RUNTIME_DIR": str(Path(xdg_env["HOME"]) / "run")}

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in tools else None

    def run(argv: Sequence[str], timeout_s: float) -> CommandResult | None:
        if argv[0] == "nvidia-smi":
            return CommandResult(0, NVIDIA_SMI, "")
        return None

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return DoctorContext(
        env=env,
        paths=SonuPaths.from_env(env),
        settings=settings or Settings(),
        settings_from_file=False,
        which=which,
        run=run,
        ollama_transport=ollama.transport if ollama else httpx.MockTransport(refuse),
        home=Path(env["HOME"]),
    )


def by_id(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {result.id: result for result in results}


@pytest.mark.anyio
async def test_machine_like_the_authors(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows = [
        make_window("0xa1", "code-oss", "main.py - sonu", workspace_id=3),
        make_window("0xb2", "zen", SECRET_TITLE, workspace_id=2),
    ]
    results = by_id(await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama())))

    assert results["gpu.nvidia"].status == "ok"
    assert "8 GiB" in results["gpu.nvidia"].summary
    assert results["hyprland.version"].status == "ok"
    assert "0.56.2" in results["hyprland.version"].summary
    assert results["hyprland.config"].details == {"config_provider": "lua"}
    assert results["hyprland.state"].summary.endswith("2 window(s)")
    assert results["hyprland.state"].details["window_classes"] == ["code-oss", "zen"]
    assert results["ollama.server"].status == "ok"

    planner = results["ollama.model.planner"]
    assert planner.status == "warn"
    assert "ollama pull qwen3.5:4b" in (planner.hint or "")
    # Only chat models with tool calling that fit in 8 GiB are suggested, biggest first —
    # never embedding models (even if they report "tools") and never never-refuse fine-tunes.
    assert planner.details["alternatives"] == ["qwen3:8b", "llama3.1:8b"]
    assert results["ollama.models"].details["tool_capable"] == [
        "gemma4:e4b",
        UNCENSORED,
        "llama3.1:8b",
        "qwen3.6:35b-a3b",
        "qwen3:8b",
    ]

    assert results["ollama.model.embed"].status == "ok"
    # wtype is missing, but it's only needed in a later milestone, so this is still "ok".
    assert results["tools.desktop"].status == "ok"
    assert results["tools.desktop"].details["missing"] == ["wtype"]


@pytest.mark.anyio
async def test_window_titles_never_appear(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows = [make_window("0xb2", "zen", SECRET_TITLE, workspace_id=2)]
    results = await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama()))
    dumped = json.dumps([result.as_dict() for result in results])
    assert "private@example.com" not in dumped


@pytest.mark.anyio
async def test_old_hyprland_uses_classic_dispatchers(xdg_env: dict[str, str], short_tmp: Path) -> None:
    async with FakeHyprland(short_tmp, signature="old", version="0.54.0", config_provider=None) as fake:
        results = by_id(await run_checks(make_context(xdg_env, hypr=fake, ollama=authors_ollama())))
    assert results["hyprland.version"].status == "warn"
    assert results["hyprland.config"].details == {"config_provider": "hyprlang"}


@pytest.mark.anyio
async def test_no_hyprland(xdg_env: dict[str, str]) -> None:
    results = by_id(await run_checks(make_context(xdg_env, hypr=None, ollama=authors_ollama())))
    assert results["hyprland.instance"].status == "fail"
    assert "hyprland.version" not in results


@pytest.mark.anyio
async def test_ollama_down(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    results = by_id(await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=None)))
    assert results["ollama.server"].status == "fail"
    assert not any(key.startswith("ollama.model") for key in results)


@pytest.mark.anyio
async def test_model_bigger_than_vram_warns(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    settings = Settings(models=ModelsSettings(planner=PlannerRole(model="qwen3.6:35b-a3b")))
    results = by_id(
        await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama(), settings=settings))
    )
    assert results["ollama.model.planner"].status == "warn"
    assert "larger than your GPU memory" in results["ollama.model.planner"].summary


@pytest.mark.anyio
async def test_planner_without_tool_calling_fails(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    settings = Settings(models=ModelsSettings(planner=PlannerRole(model="nomic-embed-text")))
    results = by_id(
        await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama(), settings=settings))
    )
    assert results["ollama.model.planner"].status == "fail"
    assert "does not support tools" in results["ollama.model.planner"].summary


@pytest.mark.anyio
async def test_installed_planner_that_fits(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    settings = Settings(models=ModelsSettings(planner=PlannerRole(model="qwen3:8b")))
    results = by_id(
        await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama(), settings=settings))
    )
    assert results["ollama.model.planner"].status == "ok"
    assert "fits in GPU memory" in results["ollama.model.planner"].summary


@pytest.mark.anyio
async def test_invalid_config_is_reported(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    context = make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama())
    context.settings_error = "config.toml: policy.autonmy: Extra inputs are not permitted"
    results = by_id(await run_checks(context))
    assert results["sonu.config"].status == "fail"


@pytest.mark.anyio
async def test_damaged_audit_log_fails(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    context = make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama())
    context.paths.ensure_private_dirs()
    context.paths.audit_file.write_text("this is not an audit record\n")
    results = by_id(await run_checks(context))
    assert results["sonu.audit"].status == "fail"


@pytest.mark.anyio
async def test_shared_directories_warn(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    context = make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama())
    context.paths.ensure_private_dirs()
    context.paths.state_dir.chmod(0o755)
    results = by_id(await run_checks(context))
    assert results["sonu.dirs"].status == "warn"


@pytest.mark.anyio
async def test_hypr_theme_rice_is_detected(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    home = Path(xdg_env["HOME"])
    defaults = home / ".config" / "hypr-theme" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "browser").write_text("zen-browser\n")
    (defaults / "terminal").write_text("kitty\n")
    (defaults / "agent").write_text("$(rm -rf ~)\n")  # not a plain app name: ignored
    (home / ".config" / "quickshell").mkdir(parents=True)
    (home / ".config" / "quickshell" / "shell.qml").write_text("// shell")
    results = by_id(await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama())))
    rice = results["integration.hypr_theme"]
    assert rice.status == "info"
    assert rice.details["defaults"] == {
        "browser": {"command": "zen-browser", "source": "file"},
        "terminal": {"command": "kitty", "source": "file"},
    }
    assert rice.details["quickshell"] is True


@pytest.mark.anyio
async def test_rice_without_default_files_uses_the_rices_fallbacks(
    xdg_env: dict[str, str], fake_hypr: FakeHyprland
) -> None:
    """The author's actual setup: hypr-theme installed, no defaults/ files yet."""
    home = Path(xdg_env["HOME"])
    (home / ".config" / "hypr-theme").mkdir(parents=True)
    results = by_id(await run_checks(make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama())))
    rice = results["integration.hypr_theme"]
    assert rice.details["defaults"] == {
        "browser": {"command": "zen-browser", "source": "rice-fallback"},
        "terminal": {"command": "kitty", "source": "rice-fallback"},
    }
    assert "zen-browser (rice default)" in rice.summary


@pytest.mark.anyio
async def test_embedding_model_as_planner_fails(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    settings = Settings(models=ModelsSettings(planner=PlannerRole(model="qwen3-embedding:0.6b")))
    context = make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama(), settings=settings)
    results = by_id(await run_checks(context))
    assert results["ollama.model.planner"].status == "fail"
    assert "embedding model" in results["ollama.model.planner"].summary


@pytest.mark.anyio
async def test_never_refuse_model_as_planner_warns(xdg_env: dict[str, str], fake_hypr: FakeHyprland) -> None:
    settings = Settings(models=ModelsSettings(planner=PlannerRole(model=UNCENSORED)))
    context = make_context(xdg_env, hypr=fake_hypr, ollama=authors_ollama(), settings=settings)
    results = by_id(await run_checks(context))
    assert results["ollama.model.planner"].status == "warn"
    assert "never refuse" in results["ollama.model.planner"].summary


@pytest.mark.anyio
async def test_crashing_check_is_contained(xdg_env: dict[str, str]) -> None:
    async def explode(context: DoctorContext) -> list[CheckResult]:
        raise RuntimeError("kaboom")

    context = make_context(xdg_env, hypr=None, ollama=None)
    results = await run_checks(context, checks=(explode, check_system))
    assert results[0].status == "fail"
    assert "kaboom" in results[0].summary
    assert results[1].id == "system.platform"


@pytest.mark.anyio
async def test_non_linux_and_old_python_fail(xdg_env: dict[str, str]) -> None:
    context = make_context(xdg_env, hypr=None, ollama=None)
    context.platform = "darwin"
    context.python_version = (3, 11, 9)
    results = by_id(await check_system(context))
    assert results["system.platform"].status == "fail"
    assert results["system.python"].status == "fail"


@pytest.mark.anyio
async def test_settings_programs_are_listed_with_packages(xdg_env: dict[str, str]) -> None:
    from sonu.doctor.checks import check_settings_programs

    context = make_context(xdg_env, hypr=None, ollama=None, tools=("wpctl", "brightnessctl", "nmcli"))
    [result] = await check_settings_programs(context)
    assert result.status == "info"
    assert result.summary.startswith("found: wpctl, brightnessctl, nmcli · missing: bluetoothctl")
    assert result.hint is not None and "bluetoothctl — Bluetooth (pacman: bluez-utils)" in result.hint
    assert "yt-dlp — playing songs and videos from YouTube (pacman: yt-dlp)" in result.hint


def test_the_rices_file_manager_comes_from_binds_lua(tmp_path: Path) -> None:
    binds = tmp_path / ".config" / "hypr" / "modules" / "binds.lua"
    binds.parent.mkdir(parents=True)
    binds.write_text('local terminal = "kitty"\nlocal fileManager = "thunar"\n')
    assert detect_hypr_theme(tmp_path).defaults["files"] == DefaultApp("thunar", "binds")
    binds.write_text('local fileManager = "thunar; rm -rf ~"\n')  # not an app name: ignored
    assert "files" not in detect_hypr_theme(tmp_path).defaults


def test_the_rices_default_apps_come_from_orrery(tmp_path: Path) -> None:
    # Found on real hardware (2026-10-04): the rice moved to ~/.config/orrery; SONU read only
    # hypr-theme, so "open a terminal" asked "Alacritty or kitty?".
    (tmp_path / ".config" / "orrery" / "defaults").mkdir(parents=True)
    (tmp_path / ".config" / "orrery" / "defaults" / "browser").write_text("zen-browser\n")
    defaults = detect_hypr_theme(tmp_path).defaults
    assert defaults["browser"] == DefaultApp("zen-browser", "file")
    assert defaults["terminal"] == DefaultApp("kitty", "rice-fallback")  # no file: the rice's own kitty


@pytest.mark.anyio
async def test_the_activity_journal_line(xdg_env: dict[str, str]) -> None:
    from sonu.config.settings import JournalSettings
    from sonu.core.journal import Journal
    from sonu.doctor.checks import check_sonu

    context = make_context(xdg_env, hypr=None, ollama=None)
    assert by_id(await check_sonu(context))["sonu.journal"].summary.startswith("nothing yet")
    journal = Journal(
        context.paths.data_dir / "journal.sqlite3", pause_file=context.paths.data_dir / "journal-paused"
    )
    journal.record("kitty", "zsh", 1.0e9)
    journal.record("zen", "News", 1.0e9 + 60)
    result = by_id(await check_sonu(context))["sonu.journal"]
    assert (result.status, result.summary) == ("ok", "2 window changes recorded · kept 30 days, local only")
    journal.pause(True)
    assert by_id(await check_sonu(context))["sonu.journal"].summary.startswith("paused")
    journal.close()
    off = make_context(
        xdg_env, hypr=None, ollama=None, settings=Settings(journal=JournalSettings(enabled=False))
    )
    assert by_id(await check_sonu(off))["sonu.journal"].summary == "off in config"


@pytest.mark.anyio
async def test_the_cloud_models_line(xdg_env: dict[str, str]) -> None:
    from sonu.config.settings import CloudSettings, PrivacySettings
    from sonu.doctor.checks import check_ollama

    async def line(settings: Settings, fake: FakeOllama) -> CheckResult:
        results = await check_ollama(make_context(xdg_env, hypr=None, ollama=fake, settings=settings))
        return by_id(results)["ollama.cloud"]

    fake = authors_ollama()
    fake.add_model("gpt-oss:120b-cloud", size_gib=60, capabilities=["completion", "tools", "thinking"])
    fake.add_model("gemma4:31b-cloud", size_gib=20, capabilities=["completion", "vision"])
    off = await line(Settings(), fake)
    assert (off.status, off.summary) == ("info", "off — nothing leaves this laptop")
    assert "POST /api/me" not in fake.requests  # off: not even asked who it's signed in as

    allow = Settings(
        privacy=PrivacySettings(cloud="allow"), cloud=CloudSettings(use_for=["prompts", "planning", "vision"])
    )
    not_signed_in = await line(allow, fake)
    assert not_signed_in.status == "warn" and "isn't signed in" in not_signed_in.summary
    assert "ollama signin" in (not_signed_in.hint or "")

    fake.cloud_user = "martin"
    ready = await line(allow, fake)
    assert ready.status == "ok"
    assert "signed in as martin" in ready.summary
    assert "planning (no window titles)" in ready.summary
    assert "gpt-oss:120b-cloud, gemma4:31b-cloud" in ready.summary

    fake.retired.add("gemma4:31b-cloud")
    retired = await line(allow, fake)
    assert retired.status == "warn" and "gemma4:31b-cloud" in retired.summary and "retired" in retired.summary

    asked = await line(Settings(privacy=PrivacySettings(cloud="ask")), fake)
    assert asked.status == "warn" and "isn't built yet" in asked.summary

    wrong = await line(Settings(models=ModelsSettings(planner=PlannerRole(model="gpt-oss:120b-cloud"))), fake)
    assert wrong.status == "fail" and "window titles too" in wrong.summary


@pytest.mark.anyio
async def test_the_cloud_line_counts_use_and_says_when_a_limit_was_reached(xdg_env: dict[str, str]) -> None:
    from sonu.audit.log import AuditLog
    from sonu.config.settings import CloudSettings, PrivacySettings
    from sonu.doctor.checks import check_ollama
    from sonu.llm.cloud import PAUSE_FILE, CloudPause

    fake = authors_ollama()
    fake.add_model("gpt-oss:120b-cloud", size_gib=60, capabilities=["completion", "tools", "thinking"])
    fake.cloud_user = "martin"
    allow = Settings(privacy=PrivacySettings(cloud="allow"), cloud=CloudSettings(use_for=["planning"]))
    ctx = make_context(xdg_env, hypr=None, ollama=fake, settings=allow)
    audit = AuditLog(ctx.paths.audit_file)
    for _ in range(3):
        audit.append("cloud.send", {"use": "planning", "model": "gpt-oss:120b-cloud", "chars": 9000})
        audit.append("cloud.answer", {"use": "planning", "tokens_in": 4000, "tokens_out": 100})
    ready = by_id(await check_ollama(ctx))["ollama.cloud"]
    assert ready.status == "ok" and "last 24 h: 3 sends (12k tokens) · 7 days: 3" in ready.summary

    CloudPause(ctx.paths.state_dir / PAUSE_FILE).start(600, "hourly usage limit")
    paused = by_id(await check_ollama(ctx))["ollama.cloud"]
    assert paused.status == "warn" and "usage limit reached — SONU stays local until" in paused.summary


@pytest.mark.anyio
async def test_the_notifications_line(xdg_env: dict[str, str]) -> None:
    from sonu.config.settings import NotificationsSettings
    from sonu.doctor.checks import check_sonu

    off = by_id(await check_sonu(make_context(xdg_env, hypr=None, ollama=None)))["sonu.notifications"]
    assert (off.status, off.summary) == (
        "info",
        "off — “what was that notification?” needs [notifications] remember = true",
    )
    on = Settings(notifications=NotificationsSettings(remember=True, keep_minutes=30))
    kept = by_id(await check_sonu(make_context(xdg_env, hypr=None, ollama=None, settings=on)))
    assert kept["sonu.notifications"].status == "warn"  # no busctl in this context's tools
    with_busctl = make_context(xdg_env, hypr=None, ollama=None, settings=on, tools=("busctl",))
    result = by_id(await check_sonu(with_busctl))["sonu.notifications"]
    assert (result.status, result.summary) == (
        "ok",
        "the latest, in memory only for 30 min, while sonud runs",
    )
