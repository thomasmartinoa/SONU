"""Turning what the user said ("VS Code", "my browser", "whatsapp") into one installed app.

Resolution is deterministic and explainable. The planner never invents commands: it passes the
user's words, and this module maps them to a desktop entry — or reports that the request is
ambiguous (e.g. both Code - OSS and VSCodium are installed) so SONU asks instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from sonu.desktop.apps.catalog import AppCatalog
from sonu.desktop.apps.classes import window_matches
from sonu.desktop.apps.entry import AppEntry

ResolutionStatus = Literal["found", "ambiguous", "not_found"]

CONFIDENT = 65

_FILLERS = {"the", "my", "a", "an", "app", "application", "program", "window"}

# Common names that don't appear literally in desktop entries.
BUILTIN_ALIASES: dict[str, tuple[str, ...]] = {
    "vs code": ("code", "code oss", "visual studio code", "vscodium", "codium"),
    "vscode": ("code", "code oss", "visual studio code", "vscodium", "codium"),
    "visual studio code": ("code", "code oss", "visual studio code", "vscodium", "codium"),
    "chrome": ("google chrome", "chromium"),
    "google chrome": ("google chrome", "chromium"),
    "zen": ("zen browser", "zen"),
}

# Generic roles → (rice default key, XDG main category).
ROLES: dict[str, tuple[str, str]] = {
    "browser": ("browser", "WebBrowser"),
    "web browser": ("browser", "WebBrowser"),
    "internet": ("browser", "WebBrowser"),
    "terminal": ("terminal", "TerminalEmulator"),
    "terminal emulator": ("terminal", "TerminalEmulator"),
    "console": ("terminal", "TerminalEmulator"),
    "file manager": ("", "FileManager"),
    "files": ("", "FileManager"),
}


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _strip_fillers(text: str) -> str:
    words = [word for word in normalize(text).split() if word not in _FILLERS]
    return " ".join(words)


def identifiers(entry: AppEntry) -> set[str]:
    stem = entry.stem
    values = {entry.name, stem, stem.rsplit(".", 1)[-1], entry.executable, entry.startup_wm_class}
    return {normalize(value) for value in values if value}


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ResolutionStatus
    query: str
    entry: AppEntry | None = None
    candidates: tuple[AppEntry, ...] = field(default_factory=tuple)
    reason: str = ""

    def describe_candidates(self) -> str:
        return ", ".join(candidate.name for candidate in self.candidates)


def _score(entry: AppEntry, query: str) -> int:
    ids = identifiers(entry)
    if query in ids:
        return 100
    alias_targets = BUILTIN_ALIASES.get(query)
    if alias_targets and ids & set(alias_targets):
        return 90
    query_tokens = set(query.split())
    name_tokens = set(normalize(entry.name).split())
    if query_tokens and query_tokens <= name_tokens:
        return 75
    # "chromium browser", "kitty terminal": the full name plus one descriptive word.
    if name_tokens and name_tokens <= query_tokens and len(query_tokens - name_tokens) == 1:
        return 70
    if len(query) >= 3 and any(identifier.startswith(query) for identifier in ids):
        return 65
    keywords = {normalize(keyword) for keyword in entry.keywords}
    if query in keywords or query == normalize(entry.generic_name):
        return 45
    if len(query) >= 4 and max(SequenceMatcher(None, query, identifier).ratio() for identifier in ids) >= 0.8:
        return 35
    return 0


class AppResolver:
    def __init__(
        self,
        catalog: AppCatalog,
        *,
        role_defaults: Mapping[str, str] | None = None,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.catalog = catalog
        self.role_defaults = dict(role_defaults or {})
        # User aliases: normalised phrase -> desktop id or executable name.
        self.aliases = {normalize(key): value for key, value in (aliases or {}).items()}

    def resolve(self, text: str) -> Resolution:
        query = _strip_fillers(text)
        if not query:
            return Resolution("not_found", text, reason="no application name given")

        alias = self.aliases.get(query)
        if alias:
            entry = self.catalog.by_id(alias) or next(iter(self.catalog.by_executable(alias)), None)
            if entry:
                return Resolution("found", text, entry, reason=f"your alias for '{query}'")

        if query in ROLES:
            return self._resolve_role(text, query)

        scored = [
            (score, entry)
            for entry in self.catalog.entries
            if (score := _score(entry, query)) and (not entry.no_display or score == 100)
        ]
        if not scored:
            return Resolution("not_found", text, reason=f"no installed application matches '{text}'")
        scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
        top_score = scored[0][0]
        best = tuple(entry for score, entry in scored if score == top_score)
        if top_score >= CONFIDENT and len(best) == 1:
            return Resolution("found", text, best[0], reason="matched by name")
        if top_score >= CONFIDENT:
            return Resolution("ambiguous", text, candidates=best, reason="several installed apps match")
        near = tuple(entry for _, entry in scored[:3])
        return Resolution("ambiguous", text, candidates=near, reason="no exact match; closest installed apps")

    def grounded_phrase(self, app: str, request: str) -> str:
        """Prefer the user's own, fuller phrase over the model's shortened one.

        Small models sometimes shorten names ("open VS Code" → app "code"), which silently turns an
        ambiguous request into a confident one. If the request contains a known phrase — a user
        alias or a built-in alias like "vs code" — that includes every word of ``app``, resolve
        that phrase instead, so SONU asks (or applies the user's alias) rather than guessing.
        """
        app_words = _strip_fillers(app).split()
        request_text = f" {normalize(request)} "
        if not app_words or not request.strip():
            return app
        best = app
        for phrase in (*self.aliases, *BUILTIN_ALIASES):
            words = phrase.split()
            if (
                len(words) > len(app_words)
                and all(word in words for word in app_words)
                and f" {phrase} " in request_text
                and len(phrase) > len(best)
            ):
                best = phrase
        return best

    def resolve_for_request(self, app: str, request: str) -> Resolution:
        return self.resolve(self.grounded_phrase(app, request))

    def labels_for_classes(self, classes: Iterable[str]) -> dict[str, str]:
        """App names for window classes, marking the user's default apps (for the planner's context)."""
        defaults: dict[str, str] = {}
        for role in ("browser", "terminal"):
            resolution = self._resolve_role(role, role)
            if resolution.entry is not None:
                defaults[resolution.entry.desktop_id] = f"your default {role}"
        labels: dict[str, str] = {}
        for window_class in set(classes):
            entry = next(
                (e for e in self.catalog.entries if not e.no_display and window_matches(e, (window_class,))),
                None,
            )
            if entry is None:
                continue
            note = defaults.get(entry.desktop_id)
            labels[window_class] = f"{entry.name}, {note}" if note else entry.name
        return labels

    def _resolve_role(self, text: str, query: str) -> Resolution:
        default_key, category = ROLES[query]
        default_command = self.role_defaults.get(default_key) if default_key else None
        if default_command:
            # The default is a command name ("zen-browser"); the entry may launch another binary
            # ("zen-bin"), so also accept an entry whose name/id normalises to the same words.
            wanted = normalize(default_command)
            matches = (
                self.catalog.by_executable(default_command)
                or [entry for entry in self.catalog.entries if entry.stem == default_command]
                or [
                    entry
                    for entry in self.catalog.entries
                    if not entry.no_display and wanted in identifiers(entry)
                ]
            )
            if matches:
                return Resolution("found", text, matches[0], reason=f"your default {default_key}")
        in_category = tuple(
            entry for entry in self.catalog.entries if category in entry.categories and not entry.no_display
        )
        if len(in_category) == 1:
            return Resolution("found", text, in_category[0], reason=f"the only installed {query}")
        if in_category:
            return Resolution(
                "ambiguous", text, candidates=in_category, reason=f"several {query} apps installed"
            )
        return Resolution("not_found", text, reason=f"no {query} is installed")
