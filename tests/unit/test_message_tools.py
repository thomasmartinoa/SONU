"""Unread messages (M7 part 1, D68): counts from chat apps' window titles, nothing else of them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sonu.config.settings import Settings
from sonu.core.engine import Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.apps.entry import AppEntry
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.policy import Policy
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.context import ToolContext
from sonu.tools.message_tools import is_chat_app, unread_in
from sonu.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    ("title", "app", "count"),
    [
        ("(3) WhatsApp", "WhatsApp Desktop", 3),
        ("WhatsApp", "WhatsApp Desktop", None),
        ("Telegram (12)", "Telegram", 12),
        ("Inbox (12) - me@example.com - Gmail", "Gmail", 12),
        ("Inbox - me@example.com - Gmail", "Gmail", None),
        ("(2) Discord | #general | A server", "Discord", 2),
        ("• Discord | #general", "Discord", -1),
        ("Alice (2)", "Telegram", None),  # a chat's name, not a count
        ("Build (2) report", "Telegram", None),
    ],
)
def test_the_count_in_a_title(title: str, app: str, count: int | None) -> None:
    assert unread_in(title, app) == count


def test_chat_apps_by_category_or_name(tmp_path: Path) -> None:
    def entry(name: str, categories: tuple[str, ...] = ()) -> AppEntry:
        return AppEntry("x.desktop", tmp_path, name, categories=categories, argv=("x",))

    assert is_chat_app(entry("WhatsApp Desktop", ("Network", "Office")))
    assert is_chat_app(entry("Gmail", ("Network", "WebBrowser")))
    assert is_chat_app(entry("Element", ("Network", "InstantMessaging")))
    assert not is_chat_app(entry("Zen Browser", ("Network", "WebBrowser")))


@pytest.fixture
async def ctx(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[ToolContext]:
    write_desktop_files(tmp_path / "share" / "applications")
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    catalog = AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    yield ToolContext(desktop, AppResolver(catalog), Settings(), which=lambda name: f"/usr/bin/{name}")


async def run(ctx: ToolContext, request: str, args: dict[str, object]) -> TaskReport:
    engine = Engine(
        planner=ScriptedPlanner({request: steps_plan(("messages.unread", args))}),
        registry=ToolRegistry(),
        ctx=ctx,
        policy=Policy("confirm-risky"),
    )

    async def never(_: object) -> bool:
        raise AssertionError("reading counts never asks")

    return await engine.run(request, confirm=never)


@pytest.mark.anyio
async def test_counts_only_and_closed_apps_are_said(ctx: ToolContext, fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows.append(make_window("0x1", "WhatsApp Desktop", "(3) WhatsApp", workspace_id=6))
    report = await run(ctx, "do I have new messages?", {})
    assert report.status == "done"
    [record] = report.steps
    assert record.outcome.summary == (
        "WhatsApp Desktop: 3 unread; Telegram isn't open, so I can't see its count"
    )
    assert not record.outcome.private


@pytest.mark.anyio
async def test_one_app_named(ctx: ToolContext, fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows.append(make_window("0x2", "TelegramDesktop", "Telegram (5)", workspace_id=4))
    fake_hypr.windows.append(make_window("0x1", "WhatsApp Desktop", "(3) WhatsApp", workspace_id=6))
    report = await run(ctx, "any unread telegram messages?", {"app": "telegram"})
    assert [r.outcome.summary for r in report.steps] == ["Telegram: 5 unread"]


@pytest.mark.anyio
async def test_the_rest_of_the_title_never_appears(ctx: ToolContext, fake_hypr: FakeHyprland) -> None:
    fake_hypr.windows.append(
        make_window("0x1", "WhatsApp Desktop", "(2) Alice: see you at 6", workspace_id=6)
    )
    report = await run(ctx, "do I have new messages?", {})
    summary = report.steps[0].outcome.summary
    assert "Alice" not in summary
    assert "WhatsApp Desktop: 2 unread" in summary


@pytest.mark.anyio
async def test_an_app_that_isnt_installed(ctx: ToolContext) -> None:
    report = await run(ctx, "any signal messages?", {"app": "signal"})
    assert report.status == "failed"
    assert report.steps[0].outcome.summary == "I don't see 'signal' installed"


@pytest.mark.anyio
async def test_emails_means_the_mail_apps(ctx: ToolContext, fake_hypr: FakeHyprland, tmp_path: Path) -> None:
    """Found on the laptop (dry run): "how many unread emails do I have?" read every chat app."""
    (tmp_path / "share" / "applications" / "gmail.desktop").write_text(
        "[Desktop Entry]\nName=Gmail\nExec=gmail-app\nType=Application\nCategories=Network;WebBrowser;\n"
        "StartupWMClass=chrome-mail.google.com__mail-Default\n"
    )
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_DATA_DIRS": str(tmp_path / "x"),
    }
    ctx.resolver = AppResolver(AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}"))
    fake_hypr.windows.append(
        make_window("0x3", "chrome-mail.google.com__mail-Default", "Inbox (7) - me@example.com - Gmail")
    )
    fake_hypr.windows.append(make_window("0x1", "WhatsApp Desktop", "(3) WhatsApp", workspace_id=6))
    report = await run(ctx, "how many unread emails do I have?", {})
    assert [r.outcome.summary for r in report.steps] == ["Gmail: 7 unread"]


@pytest.mark.anyio
async def test_an_app_the_user_didnt_name_is_left_out(ctx: ToolContext, fake_hypr: FakeHyprland) -> None:
    """Found in the evals (v32): "do I have any new messages?" was planned with app "whatsapp desktop"
    (from the window list) and failed. Only the user's words name an app."""
    fake_hypr.windows.append(make_window("0x1", "WhatsApp Desktop", "(3) WhatsApp", workspace_id=6))
    report = await run(ctx, "do I have any new messages?", {"app": "whatsapp desktop"})
    assert report.status == "done"
    assert [r.outcome.summary for r in report.steps] == [
        "WhatsApp Desktop: 3 unread; Telegram isn't open, so I can't see its count"
    ]
    report = await run(ctx, "any whatsapp messages?", {"app": "whatsapp desktop"})
    assert [r.outcome.summary for r in report.steps] == ["WhatsApp Desktop: 3 unread"]
