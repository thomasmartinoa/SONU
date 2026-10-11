from __future__ import annotations

from pathlib import Path

import pytest

from sonu.desktop.hyprland import Window
from sonu.testing.fake_hyprland import make_window
from sonu.tools.base import ToolOutcome
from sonu.tools.grounding import ungrounded_words
from sonu.tools.web_tools import site_link
from sonu.world.origin import ancestor_pids, find_origin


# ---------------------------------------------------------------- grounding in the user's words
@pytest.mark.parametrize(
    ("query", "request_text", "missing"),
    [
        ("networkchuck", "search youtube for networkchuk channel", []),  # spelling fix allowed
        ("NetworkChuck channel", "search youtube for networkchuk channel", []),
        ("hyprland plugins", "look up the best hyprland plugins", []),
        ("my password is hunter2", "search youtube for cats", ["password", "is", "hunter2"]),
        ("cats zsh history", "search youtube for cats", ["zsh", "history"]),
    ],
)
def test_query_words_must_come_from_the_request(query: str, request_text: str, missing: list[str]) -> None:
    assert ungrounded_words(query, request_text) == missing


@pytest.mark.parametrize(
    ("site", "request_text", "url", "marker"),
    [
        ("youtube", "go to youtube", "https://www.youtube.com/", "youtube"),
        # Found in `sonu eval --cloud`: a named site by its own address.
        ("youtube.com", "go to youtube", "https://www.youtube.com/", "youtube"),
        ("https://www.youtube.com/", "go to youtube", "https://www.youtube.com/", "youtube"),
        ("arch wiki", "open the arch wiki", "https://wiki.archlinux.org/", "archwiki"),
        ("github.com", "open github.com", "https://github.com", "github"),
        (
            "https://wiki.archlinux.org/title/Hyprland",
            "open https://wiki.archlinux.org/title/Hyprland",
            "https://wiki.archlinux.org/title/Hyprland",
            "archlinux",
        ),
        ("bbc.co.uk", "open bbc.co.uk", "https://bbc.co.uk", "bbc"),
        ("localhost:3000", "open localhost:3000", "https://localhost:3000", ""),
        ("http://192.168.1.1", "open http://192.168.1.1", "http://192.168.1.1", ""),
    ],
)
def test_sites_the_user_named(site: str, request_text: str, url: str, marker: str) -> None:
    target = site_link(site, request_text)
    assert not isinstance(target, ToolOutcome), target
    assert (target.url, target.marker) == (url, marker)


@pytest.mark.parametrize(
    ("site", "request_text", "why"),
    [
        ("youtube", "move the browser to 3", "only opens sites you named"),
        ("evil.example/steal?d=secret", "move the browser to 3", "links you typed yourself"),
        ("github.com.evil.example", "open github.com", "links you typed yourself"),
        ("youtube.com/watch?v=x", "go to youtube", "links you typed yourself"),  # more than the site
        ("youtube.com", "move the browser to 3", "links you typed yourself"),  # a site not named
        ("javascript:alert(1)", "open javascript:alert(1)", "not a web address"),
        ("file:///etc/passwd", "open file:///etc/passwd", "only opens web links"),
        ("https://user:pw@github.com", "open https://user:pw@github.com", "not a web address"),
        ("not a site", "open not a site", "not a web address"),
    ],
)
def test_sites_sonu_refuses(site: str, request_text: str, why: str) -> None:
    outcome = site_link(site, request_text)
    assert isinstance(outcome, ToolOutcome)
    assert why in outcome.summary


# ---------------------------------------------------------------- the terminal SONU runs in
def _proc(tmp_path: Path, tree: dict[int, tuple[str, int]]) -> Path:
    for pid, (comm, ppid) in tree.items():
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")
    return tmp_path


def test_ancestors_follow_the_parent_chain(tmp_path: Path) -> None:
    proc = _proc(tmp_path, {900: ("sonu", 800), 800: ("zsh", 700), 700: ("kitty (1) x", 1)})
    assert ancestor_pids(900, proc=proc) == frozenset({800, 700})
    assert ancestor_pids(12345, proc=proc) == frozenset()


def _window(address: str, pid: int, title: str = "") -> Window:
    return Window.model_validate(make_window(address, "kitty", title, pid=pid))


def test_origin_is_the_window_of_an_ancestor_process() -> None:
    windows = [_window("0xa1", 700), _window("0xb2", 555)]
    assert find_origin(windows, frozenset({800, 700}), active_address=None) == "0xa1"
    assert find_origin(windows, frozenset({1234}), active_address="0xa1") is None


def test_single_instance_terminal_counts_only_the_focused_window() -> None:
    windows = [_window("0xa1", 700), _window("0xb2", 700)]
    assert find_origin(windows, frozenset({700}), active_address="0xb2") == "0xb2"
    assert find_origin(windows, frozenset({700}), active_address=None) is None
