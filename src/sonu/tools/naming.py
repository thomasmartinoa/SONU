"""Finding a control by the user's words for it — a button in an app (``screen.press``, D56) or a link,
button or field on a web page (``browser.click`` / ``browser.fill``, D72) — and the names that make a press
high risk."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Buttons whose press can't be taken back: asked like other high-risk steps (by voice: on screen).
RISKY_NAMES = re.compile(
    r"\b(delete|remove|erase|format|send|submit|post|publish|buy|pay|purchase|order|checkout|confirm|"
    r"merge|uninstall|install|sign out|log ?out|shut ?down|restart|discard|overwrite|transfer)\b",
    re.I,
)


# What people add to a control's name ("the home button", "the search box"); found on the laptop: "home
# button" didn't match Thunar's "Home".
KIND_WORDS = {
    "the",
    "a",
    "button",
    "buttons",
    "icon",
    "tab",
    "menu",
    "link",
    "item",
    "toggle",
    "switch",
    "key",
    "option",
    "field",
    "box",
    "input",
    "bar",
    "checkbox",
}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def name_score(name: str, wanted: str) -> int:
    """How well a control's name matches the user's words: 100 the same, 60 it starts with them, 40 it has
    them all, 30 nearly the same spelling, 0 not it."""
    name = name.lower().strip()
    words = [w for w in _words(wanted) if w not in KIND_WORDS] or _words(wanted)
    wanted = " ".join(words)
    if name == wanted or _words(name) == words:
        return 100
    if words and _words(name)[: len(words)] == words:
        return 60
    if words and all(w in _words(name) for w in words):
        return 40
    if SequenceMatcher(None, name, wanted.lower()).ratio() >= 0.8:
        return 30
    return 0
