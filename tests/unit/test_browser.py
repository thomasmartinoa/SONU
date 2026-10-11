"""SONU's browser extension (D70): the native messaging host, the setup, and the tab tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import sys
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sonu.browser.host import browser_of
from sonu.browser.install import CHROMIUM_ID, GECKO_ID, extension_files, install, plan, uninstall
from sonu.config.settings import AppsSettings, Settings, VerifySettings
from sonu.core.engine import ConfirmRequest, Engine
from sonu.core.report import TaskReport
from sonu.desktop.apps import AppCatalog, AppResolver
from sonu.desktop.hyprland.desktop import HyprlandDesktop
from sonu.integrations.browser import Browsers, Tab
from sonu.policy import Policy
from sonu.testing.fake_browser import FakeBrowser, FakeTab
from sonu.testing.fake_hyprland import FakeHyprland, make_window
from sonu.testing.fake_planner import ScriptedPlanner, steps_plan
from sonu.tools.base import Risk
from sonu.tools.browser_tools import matching
from sonu.tools.context import ToolContext
from sonu.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------- the extension and setup
def test_the_extensions_ids_are_fixed() -> None:
    manifest = json.loads(extension_files()["manifest.json"])
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == CHROMIUM_ID
    assert manifest["browser_specific_settings"]["gecko"]["id"] == GECKO_ID
    assert set(manifest["permissions"]) == {"nativeMessaging", "tabs"}  # no page access in part 1


def test_install_writes_only_for_the_browsers_there(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    (home / ".config" / "chromium").mkdir(parents=True)
    (home / ".config" / "zen").mkdir(parents=True)
    setup = plan(home, data)
    assert setup.manifests == (
        home / ".config/chromium/NativeMessagingHosts/dev.sonu.browser.json",
        home / ".mozilla/native-messaging-hosts/dev.sonu.browser.json",
    )
    install(setup, python="/usr/bin/python3")
    chromium = json.loads(setup.manifests[0].read_text())
    assert chromium["allowed_origins"] == [f"chrome-extension://{CHROMIUM_ID}/"]
    assert chromium["path"] == str(setup.launcher)
    assert json.loads(setup.manifests[1].read_text())["allowed_extensions"] == [GECKO_ID]
    assert setup.launcher.read_text() == "#!/bin/sh\nexec '/usr/bin/python3' -I -m sonu.browser.host \"$@\"\n"
    assert zipfile.ZipFile(setup.xpi).namelist() == ["manifest.json", "background.js"]
    assert (setup.extension / "background.js").exists()
    assert not (home / ".config" / "google-chrome").exists()  # not a browser the user has
    uninstall(setup)
    assert not any(path.exists() for path in setup.manifests) and not setup.launcher.exists()


def test_the_browser_is_the_hosts_parent(tmp_path: Path) -> None:
    for exe, name in (("/opt/zen-browser-bin/zen-bin", "zen"), ("/usr/lib/chromium/chromium", "chromium")):
        proc = tmp_path / name / "42"
        proc.mkdir(parents=True)
        (proc / "exe").symlink_to(exe)
        assert browser_of(42, tmp_path / name) == name
    assert browser_of(7, tmp_path) == "browser"


# ---------------------------------------------------------------------------- the host
def frame(message: dict[str, Any]) -> bytes:
    data = json.dumps(message).encode()
    return struct.pack("=I", len(data)) + data


async def read_frame(stream: asyncio.StreamReader) -> dict[str, Any]:
    (length,) = struct.unpack("=I", await stream.readexactly(4))
    result: dict[str, Any] = json.loads(await stream.readexactly(length))
    return result


@pytest.mark.anyio
async def test_the_host_relays_sonus_requests_to_the_extension(short_tmp: Path) -> None:
    """The real host as the browser starts it; the test plays the extension."""
    env = {"XDG_RUNTIME_DIR": str(short_tmp), "PATH": "/usr/bin:/bin"}
    host = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sonu.browser.host",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    assert host.stdin is not None and host.stdout is not None
    host.stdin.write(frame({"hello": {"browser": "firefox", "version": "0.1.0"}}))
    await host.stdin.drain()
    folder = short_tmp / "sonu" / "browsers"
    for _ in range(100):
        if list(folder.glob("*.sock")):
            break
        await asyncio.sleep(0.05)
    [sock] = list(folder.glob("*.sock"))
    assert oct(sock.stat().st_mode & 0o777) == "0o600" and oct(folder.stat().st_mode & 0o777) == "0o700"
    browsers = Browsers(folder)
    [link] = await browsers.links()
    assert link.version == "0.1.0"
    asked = asyncio.create_task(browsers.tabs([link]))
    request = await read_frame(host.stdout)
    assert request["op"] == "tabs"
    tab = {
        "id": 3,
        "window": 1,
        "index": 0,
        "active": True,
        "title": "YouTube",
        "url": "https://www.youtube.com/",
    }
    host.stdin.write(frame({"id": request["id"], "ok": True, "result": {"tabs": [tab]}}))
    await host.stdin.drain()
    [found] = await asyncio.wait_for(asked, 5)
    assert (found.title, found.host, found.active) == ("YouTube", "youtube.com", True)
    host.stdin.close()  # the browser quits
    await asyncio.wait_for(host.wait(), 5)
    assert not sock.exists()


# ---------------------------------------------------------------------------- the tools
def test_matching_takes_every_word_from_the_title_or_site(tmp_path: Path) -> None:
    def tab(title: str, url: str) -> Tab:
        return Tab("zen", tmp_path, 1, 1, 0, False, title, url)

    tabs = [
        tab("Faded - YouTube", "https://www.youtube.com/watch?v=1"),
        tab("Pull requests · thomasmartinoa/SONU", "https://github.com/thomasmartinoa/SONU/pulls"),
        tab("Inbox", "https://mail.google.com/"),
    ]
    assert [t.title for t in matching(tabs, "the youtube tab")] == ["Faded - YouTube"]
    assert [t.title for t in matching(tabs, "github pull requests")] == [
        "Pull requests · thomasmartinoa/SONU"
    ]
    assert matching(tabs, "the tab") == []


class Rig:
    def __init__(self, fake: FakeHyprland, ctx: ToolContext, browser: FakeBrowser) -> None:
        self.fake, self.ctx, self.browser = fake, ctx, browser
        self.confirmations: list[ConfirmRequest] = []

    async def run(self, request: str, tool: str, args: dict[str, Any], approve: bool = True) -> TaskReport:
        async def confirm(confirm_request: ConfirmRequest) -> bool:
            self.confirmations.append(confirm_request)
            return approve

        engine = Engine(
            planner=ScriptedPlanner({request: steps_plan((tool, args))}),
            registry=ToolRegistry(),
            ctx=self.ctx,
            policy=Policy("confirm-risky"),
        )
        return await engine.run(request, confirm=confirm)


@pytest.fixture
async def rig(fake_hypr: FakeHyprland, short_tmp: Path) -> AsyncIterator[Rig]:
    fake_hypr.windows.append(make_window("0x10", "kitty", "zsh", workspace_id=1))
    fake_hypr.windows.append(make_window("0x20", "zen", "Inbox — Zen Browser", workspace_id=2))
    fake_hypr.active_address = "0x10"
    tabs = [
        FakeTab(1, "Inbox", "https://mail.google.com/", active=True),
        FakeTab(2, "Faded - YouTube", "https://www.youtube.com/watch?v=1"),
        FakeTab(3, "Alone - YouTube", "https://www.youtube.com/watch?v=2"),
        FakeTab(4, "My bank account", "https://bank.example/"),
    ]

    def shown(tab: FakeTab) -> None:  # the browser's window shows the active tab's title
        window = next(w for w in fake_hypr.windows if w["class"] == "zen")
        window["title"] = f"{tab.title} — Zen Browser"

    async with FakeBrowser(short_tmp / "browsers", tabs=tabs, on_activate=shown) as browser:
        desktop = await HyprlandDesktop.connect(fake_hypr.instance)
        ctx = ToolContext(
            desktop,
            AppResolver(AppCatalog(())),
            Settings(apps=AppsSettings(launch_timeout_s=1.0), verify=VerifySettings(timeout_s=0.5)),
            browsers=Browsers(short_tmp / "browsers"),
        )
        yield Rig(fake_hypr, ctx, browser)


def summaries(report: TaskReport) -> list[str]:
    return [record.outcome.summary for record in report.steps]


@pytest.mark.anyio
async def test_switch_to_a_tab_by_the_users_words(rig: Rig) -> None:
    report = await rig.run("switch to the faded tab", "browser.tab", {"words": "faded"})
    assert report.status == "done", summaries(report)
    assert summaries(report) == ["Switched to the tab matching 'faded' in zen"]
    assert [t.id for t in rig.browser.tabs if t.active] == [2]
    assert rig.fake.active_address == "0x20"  # SONU focused the browser's window itself
    assert "Faded" not in report.steps[0].description  # titles stay out of the description


@pytest.mark.anyio
async def test_two_matching_tabs_are_asked_about(rig: Rig) -> None:
    report = await rig.run("switch to the youtube tab", "browser.tab", {"words": "youtube"})
    assert report.status == "needs-input"
    choices = report.steps[0].outcome.choices
    assert choices is not None
    assert [o.label for o in choices.options] == [
        "Faded - YouTube — youtube.com",
        "Alone - YouTube — youtube.com",
    ]


@pytest.mark.anyio
async def test_close_shows_the_tabs_and_asks(rig: Rig) -> None:
    report = await rig.run(
        "close all the youtube tabs", "browser.close_tabs", {"words": "youtube", "all": True}
    )
    assert report.status == "done", summaries(report)
    [asked] = rig.confirmations
    assert asked.risk is Risk.MEDIUM
    assert asked.description == "Close 2 tabs matching 'youtube'"
    assert asked.details == "Faded - YouTube — youtube.com\nAlone - YouTube — youtube.com"
    assert [t.id for t in rig.browser.tabs] == [1, 4]
    assert summaries(report) == ["Closed 2 tabs matching 'youtube'"]


@pytest.mark.anyio
async def test_nothing_closes_when_the_user_says_no(rig: Rig) -> None:
    report = await rig.run("close the bank tab", "browser.close_tabs", {"words": "bank"}, approve=False)
    assert report.status == "declined"
    assert len(rig.browser.tabs) == 4


@pytest.mark.anyio
async def test_the_list_is_private(rig: Rig) -> None:
    report = await rig.run("what tabs do I have open?", "browser.tabs", {})
    outcome = report.steps[0].outcome
    assert outcome.private
    assert outcome.summary.startswith("zen: 4 tabs — Inbox — mail.google.com (this one); Faded - YouTube")


@pytest.mark.anyio
async def test_words_that_arent_the_users(rig: Rig) -> None:
    report = await rig.run("switch tabs", "browser.tab", {"words": "bank"})
    assert report.status == "failed"
    assert [t.id for t in rig.browser.tabs if t.active] == [1]


@pytest.mark.anyio
async def test_no_extension_connected(fake_hypr: FakeHyprland, short_tmp: Path) -> None:
    desktop = await HyprlandDesktop.connect(fake_hypr.instance)
    ctx = ToolContext(desktop, AppResolver(AppCatalog(())), Settings(), browsers=Browsers(short_tmp / "none"))
    rig = Rig(fake_hypr, ctx, FakeBrowser(short_tmp))
    report = await rig.run("what tabs do I have open?", "browser.tabs", {})
    assert report.status == "failed"
    assert "sonu browser install" in summaries(report)[0]
