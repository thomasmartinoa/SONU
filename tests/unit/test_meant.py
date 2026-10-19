"""The names the user meant, matched by sound against what exists (D107) — from the owner's recordings."""

from __future__ import annotations

from pathlib import Path

import pytest

from sonu.desktop.apps.catalog import AppCatalog
from sonu.desktop.apps.resolver import AppResolver
from sonu.evals.runner import load_cases
from sonu.testing.fake_apps import write_desktop_files
from sonu.voice import bench
from sonu.voice.meant import Names, known_names, project_names, sound_key

# Like the owner's laptop: apps (names and programs), Claude Code, their projects.
OWNER = Names.of(
    [
        "Claude Code", "Claude", "btop++", "btop", "Neovim", "nvim", "Thunar File Manager", "thunar",
        "Zen Browser", "zen-browser", "zen", "kitty", "Telegram", "Visual Studio Code", "code", "vs code",
        "vlc", "VLC media player", "Bulk Rename", "OBS Studio", "obs", "Spotify (Launcher)",
        "spotify-launcher", "Chromium", "chromium", "Bluetooth Manager",
        "dev_works", "sonu-test", "SONU", "shop-app",
    ]
)  # fmt: skip


@pytest.mark.parametrize(
    ("heard", "meant"),
    [
        # What Whisper and Sarvam wrote for the owner (`sonu voice bench`, 2026-10-19), and what failed:
        ("Open Cloud Code in Dubworks", "Open Claude Code in dev_works"),  # 'cloud-code' is not installed
        ("Open browser in 4 and EnVim in 5 and Cloud Code in 5.",
         "Open browser in 4 and nvim in 5 and Claude Code in 5."),
        ("Open VTOP", "Open btop"),  # 'vtop' is not installed
        ("Open NWIM in 5", "Open nvim in 5"),
        ("Open Tunar in downloads.", "Open thunar in downloads."),
        ("Open Zenon Workspace 4", "Open Zen on Workspace 4"),  # SONU only opens links you typed
        ("open cloud code in sonu-test", "open Claude Code in sonu-test"),
    ],
)  # fmt: skip
def test_a_name_heard_another_way_is_the_name_that_exists(heard: str, meant: str) -> None:
    assert OWNER.meant(heard)[0] == meant


def test_what_was_heard_right_is_never_changed() -> None:
    """Every request of the eval suite and every bench line, as said: nothing changes."""
    for text in [case.request for case in load_cases()] + [line.text for line in bench.LINES]:
        assert OWNER.meant(text) == (text, []), text


@pytest.mark.parametrize(
    "heard",
    [
        "Move Send to 3",  # Zen, misheard — but "send" is a word: never guessed
        "Open nvim in file",  # five, misheard — a word too
        "switch to the Cloud",  # SONU's cloud settings, not the Claude app
        "Cloud",
        "Open Bulk Rename",  # not vlc ("vee el see")
    ],
)
def test_everyday_words_stay(heard: str) -> None:
    assert OWNER.meant(heard) == (heard, [])


def test_two_names_that_sound_alike_are_never_guessed_between() -> None:
    names = Names.of(["Telegram", "Telegrem"])
    assert names.meant("Open Telegrom") == ("Open Telegrom", [])
    assert sound_key("Claude Code") == sound_key("Cloud Code") and sound_key("VTOP") == sound_key("btop")


def test_the_changes_are_said() -> None:
    assert OWNER.meant("Open Cloud Code in Dubworks")[1] == [
        ("Cloud Code", "Claude Code"),
        ("Dubworks", "dev_works"),
    ]


def test_projects_are_folders_with_made_up_names_or_git(tmp_path: Path) -> None:
    for folder in (
        "dev_works/shop-app",
        "sonu-test",
        "SONU/.git",
        "Videos",
        "theme",
        "notes/.git",
        ".hidden/x-y",
    ):
        (tmp_path / folder).mkdir(parents=True)
    assert sorted(project_names(tmp_path)) == ["SONU", "dev_works", "notes", "shop-app", "sonu-test"]


def test_known_names_are_this_laptops(tmp_path: Path) -> None:
    apps = tmp_path / "data" / "applications"
    write_desktop_files(apps)
    (tmp_path / "home" / "dev_works").mkdir(parents=True)
    catalog = AppCatalog.load(
        {"XDG_DATA_HOME": str(tmp_path / "data"), "XDG_DATA_DIRS": str(tmp_path / "none")}
    )
    names = known_names(AppResolver(catalog), tmp_path / "home")
    assert names.meant("Open VTOP and Cloud Code in Dubworks")[0] == "Open btop and Claude Code in dev_works"
    assert names.meant("Open Zenon 2")[0] == "Open Zen on 2"  # "zen": SONU's own name for Zen Browser
