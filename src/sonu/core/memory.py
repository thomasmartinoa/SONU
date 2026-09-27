"""What the user asked SONU to remember ("remember …"), kept until they say "forget …".

Three kinds, each with a real effect:

* **alias** — "when I say VS Code I mean Code - OSS": the words → an installed app's desktop id; the
  resolver uses it, so SONU (not the model) decides what "VS Code" opens.
* **name** — "call me Martin": the replies use it now and then.
* **note** — anything else, in the user's own words ("the shop project is in dev_works/shop-app"):
  the planner sees the notes as data.

Stored in SONU's data directory (0600). Only the user's own words and SONU's choice of app are kept.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

MemoryKind = Literal["alias", "name", "note"]
MAX_ITEMS = 100


@dataclass(frozen=True, slots=True)
class MemoryItem:
    kind: MemoryKind
    text: str  # alias: the user's words ("vs code"); name: the name; note: the note
    app: str = ""  # alias: the desktop id it means ("code-oss.desktop")
    app_name: str = ""  # alias: how to say it ("Code - OSS")
    at: float = 0.0

    def describe(self) -> str:
        if self.kind == "alias":
            return f"“{self.text}” means {self.app_name}"
        if self.kind == "name":
            return f"your name is {self.text}"
        return self.text


@dataclass
class Memory:
    path: Path | None = None  # None: in memory only (tests)
    items: list[MemoryItem] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Memory:
        memory = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            memory.items = [
                MemoryItem(
                    item["kind"],
                    str(item["text"]),
                    str(item.get("app", "")),
                    str(item.get("app_name", "")),
                    float(item.get("at", 0)),
                )
                for item in raw
                if item.get("kind") in ("alias", "name", "note")
            ]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            memory.items = []
        return memory

    # -------------------------------------------------------------- reading
    def aliases(self) -> dict[str, str]:
        return {item.text: item.app for item in self.items if item.kind == "alias"}

    def name(self) -> str | None:
        return next((item.text for item in reversed(self.items) if item.kind == "name"), None)

    def notes(self) -> list[str]:
        return [item.describe() for item in self.items if item.kind != "name"]

    # -------------------------------------------------------------- changing
    def add(self, item: MemoryItem) -> None:
        """A new alias or name replaces the old one for the same words; notes pile up."""
        kept = [
            old
            for old in self.items
            if not (old.kind == item.kind == "name")
            and not (old.kind == item.kind == "alias" and old.text == item.text)
            and not (old.kind == item.kind == "note" and old.text.casefold() == item.text.casefold())
        ]
        self.items = [
            *kept,
            MemoryItem(item.kind, item.text, item.app, item.app_name, item.at or time.time()),
        ][-MAX_ITEMS:]
        self.save()

    def remove(self, items: list[MemoryItem]) -> None:
        self.items = [item for item in self.items if item not in items]
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(item) for item in self.items], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.chmod(0o600)
        tmp.replace(self.path)
