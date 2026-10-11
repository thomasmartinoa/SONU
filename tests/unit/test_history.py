"""History and bookmarks from the browsers' own profile folders (D73)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sonu.config.settings import AppsSettings, Settings, VerifySettings, WebSettings
from sonu.core.conversation import Conversation
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.browser_data import bookmarks, history, profiles
from sonu.policy import Policy
from sonu.system import SystemControl
from sonu.testing.fake_apps import write_desktop_files
from sonu.testing.fake_hyprland import FakeApp, FakeHyprland
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.testing.fake_profiles import chromium_profile, firefox_profile
from sonu.testing.fake_system import FakeSystem
from sonu.tools.base import Risk
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry

DAY = 24 * 60


@pytest.fixture(autouse=True)
def copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path / "run" / "sonu" / "copies"


def test_profiles_and_reading(tmp_path: Path, copies: Path) -> None:
    home = tmp_path / "home"
    firefox_profile(
        home,
        [
            ("Lentil soup recipe", "https://food.example/lentil-soup", 30),
            ("Inbox", "https://mail.example/", 5),
        ],
        [("ArchWiki: Hyprland", "https://wiki.archlinux.org/title/Hyprland", 3 * DAY)],
    )
    chromium_profile(
        home,
        [("Red lentil dal", "https://other.example/dal", DAY + 60)],
        [("SONU on GitHub", "https://github.com/thomasmartinoa/SONU", 10)],
    )
    zen, chromium = profiles(home)
    assert (zen.browser, chromium.browser) == ("zen", "chromium")
    now = time.time()
    [soup] = history(zen, copies, ["lentil"], now - DAY * 60, now)
    assert (soup.title, soup.kind) == ("Lentil soup recipe", "history")
    assert [s.title for s in history(chromium, copies, ["LENTIL"], 0, now)] == ["Red lentil dal"]
    assert history(chromium, copies, ["lentil"], now - 60 * 60, now) == []  # not in the last hour
    assert [s.url for s in bookmarks(zen, copies, ["arch", "hyprland"])] == [
        "https://wiki.archlinux.org/title/Hyprland"
    ]
    assert [s.title for s in bookmarks(chromium, copies, ["sonu"])] == ["SONU on GitHub"]
    assert history(zen, copies, ["50%_off"], 0, now) == []  # LIKE's own characters are just characters
    assert list(copies.iterdir()) == []  # every copy deleted


def test_the_latest_visits_in_the_write_ahead_log(tmp_path: Path, copies: Path) -> None:
    """Zen keeps its database open in WAL mode: the newest visits are only in places.sqlite-wal until a
    checkpoint (measured on the laptop: a 2 MB -wal beside it). SONU copies both."""
    home = tmp_path / "home"
    folder = firefox_profile(home, [("Old page", "https://old.example/", 60)])
    browser = sqlite3.connect(folder / "places.sqlite")  # the running browser
    browser.execute("PRAGMA wal_autocheckpoint = 0")
    browser.execute(
        "INSERT INTO moz_places (url, title, visit_count, last_visit_date) VALUES (?, ?, 1, ?)",
        ("https://new.example/", "Brand new page", int(time.time() * 1e6)),
    )
    browser.commit()
    try:
        assert (folder / "places.sqlite-wal").stat().st_size > 0
        [zen] = profiles(home)
        assert [s.title for s in history(zen, copies, ["brand"], 0, time.time() + 60)] == ["Brand new page"]
    finally:
        browser.close()


class Rig:
    def __init__(self, fake: FakeHyprland, ctx: ToolContext, home: Path) -> None:
        self.fake, self.ctx, self.home = fake, ctx, home
        self.asked: list[ConfirmRequest] = []

    async def run(self, request: str, tool: str, args: dict[str, Any], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.asked.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan((tool, args))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, tmp_path: Path) -> AsyncIterator[Rig]:
    home = tmp_path / "home"
    write_desktop_files(tmp_path / "share" / "applications")
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "share"), "XDG_DATA_DIRS": str(tmp_path / "x")}
    # The fake browser's window title shows the page's address and this title once it loads.
    fake_hypr.apps.update(
        {
            "zen-browser": FakeApp("zen", "Lentil soup recipe"),
            "chromium": FakeApp("chromium", "SONU on GitHub"),
        }
    )
    firefox_profile(
        home,
        [
            ("Lentil soup recipe", "https://food.example/lentil-soup", DAY),  # yesterday at any hour
            ("Lentil salad", "https://food.example/lentil-salad", DAY + 1),
            ("Inbox (3)", "https://mail.example/", 5),
            ("Reset your password", "https://bank.example/reset?token=abc", 50),
        ],
        [("ArchWiki: Hyprland", "https://wiki.archlinux.org/title/Hyprland", 3 * DAY)],
    )
    chromium_profile(home, [], [("SONU on GitHub", "https://github.com/thomasmartinoa/SONU", 10)])
    ctx = ToolContext(
        await HyprlandDesktop.connect(fake_hypr.instance),
        AppResolver(
            AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}"),
            role_defaults={"browser": "zen-browser"},
        ),
        Settings(
            apps=AppsSettings(launch_timeout_s=1.5),
            verify=VerifySettings(timeout_s=1.0),
            web=WebSettings(load_timeout_s=2.0),
        ),
        which=lambda name: f"/usr/bin/{name}",
        system=SystemControl(FakeSystem(), home=home),
    )
    yield Rig(fake_hypr, ctx, home)


def summaries(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_find_a_page_in_the_history(rig: Rig) -> None:
    report = await rig.run(
        "find the page about lentil soup I saw yesterday",
        "browser.history",
        {"words": "page about lentil soup", "when": "yesterday"},
    )
    outcome = report.steps[0].outcome
    assert report.status == "done" and outcome.private
    assert outcome.summary.startswith(
        "1 page in your history for “page about lentil soup” (yesterday): Lentil soup recipe — food.example"
    )
    assert report.steps[0].description == "Search your history for “page about lentil soup”"


@pytest.mark.anyio
async def test_bookmarks_from_every_browser(rig: Rig) -> None:
    report = await rig.run("what bookmarks do I have?", "browser.history", {"kind": "bookmarks"})
    summary = report.steps[0].outcome.summary
    assert summary.startswith(
        "2 bookmarks: SONU on GitHub — github.com; ArchWiki: Hyprland — wiki.archlinux.org"
    )
    report = await rig.run(
        "my bookmarks for gitlab", "browser.history", {"words": "gitlab", "kind": "bookmarks"}
    )
    assert summaries(report) == ["No bookmarks for “gitlab”"]


@pytest.mark.anyio
async def test_open_a_bookmark_without_asking(rig: Rig) -> None:
    report = await rig.run("open my sonu bookmark", "browser.open_saved", {"words": "sonu"})
    assert report.status == "done", summaries(report)
    assert rig.asked == []
    assert list(rig.fake.launched[-1]) == ["/usr/bin/chromium", "https://github.com/thomasmartinoa/SONU"]
    assert summaries(report) == ["Opened your bookmark matching “sonu” in Chromium"]


@pytest.mark.anyio
async def test_a_page_from_history_is_shown_and_asked(rig: Rig) -> None:
    report = await rig.run(
        "open that lentil soup page I saw yesterday",
        "browser.open_saved",
        {"words": "lentil soup", "when": "yesterday"},
    )
    assert report.status == "done", summaries(report)
    [asked] = rig.asked
    assert asked.risk is Risk.MEDIUM
    assert asked.details == "Lentil soup recipe\nhttps://food.example/lentil-soup"
    assert asked.description == "Open your history matching “lentil soup” in Zen Browser"  # no title
    assert rig.fake.launched[-1][-1] == "https://food.example/lentil-soup"


@pytest.mark.anyio
async def test_several_pages_are_asked_about(rig: Rig) -> None:
    report = await rig.run("open the lentil page from my history", "browser.open_saved", {"words": "lentil"})
    assert report.status == "needs-input"
    choices = report.steps[0].outcome.choices
    assert choices is not None
    assert sorted(o.label for o in choices.options) == [
        "Lentil salad — food.example",
        "Lentil soup recipe — food.example",
    ]


@pytest.mark.anyio
async def test_declined_opens_nothing_and_words_must_be_the_users(rig: Rig) -> None:
    report = await rig.run(
        "open the password page", "browser.open_saved", {"words": "password"}, approve=False
    )
    assert report.status == "declined" and rig.fake.launched == []
    report = await rig.run("open my bookmark", "browser.open_saved", {"words": "bank"})
    assert report.status == "failed" and rig.fake.launched == []


@pytest.mark.anyio
async def test_a_search_uses_this_requests_words(rig: Rig) -> None:
    """Found on the laptop: "what pages did I visit today?" after a lentil soup search was planned with words
    "lentil soup" (the earlier request's)."""
    first, second = "find the page about lentil soup", "what pages did I visit today?"
    engine = Engine(
        planner=ScriptedPlanner(
            {
                first: steps_plan(("browser.history", {"words": "lentil soup"})),
                second: steps_plan(("browser.history", {"words": "lentil soup", "when": "today"})),
            }
        ),
        registry=ToolRegistry(),
        ctx=rig.ctx,
        policy=Policy("confirm-risky"),
        conversation=Conversation(),
    )

    async def confirm(_: ConfirmRequest) -> bool:
        return True

    await engine.run(first, confirm=confirm)
    report = await engine.run(second, confirm=confirm)
    assert report.status == "done", summaries(report)
    assert summaries(report)[0].startswith("2 pages in your history (today): Inbox (3) — mail.example")
