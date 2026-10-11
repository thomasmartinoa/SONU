"""Saved window layouts: "save this layout as work", later "restore work" (D67).

A layout is which app is on which workspace — the app by its desktop id, so SONU can start it again
— and, for a terminal, the folder its shell was in. Never window titles (they're personal, and
restoring doesn't need them). Stored in SONU's data directory (0600), shared by sonud and the CLI.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_LAYOUTS = 20
MAX_WINDOWS = 12


@dataclass(frozen=True, slots=True)
class Slot:
    app: str  # desktop id ("kitty.desktop")
    app_name: str  # how to say it ("kitty")
    workspace: str  # "3"
    folder: str = ""  # a terminal's folder (inside the home folder), or ""


@dataclass(frozen=True, slots=True)
class Layout:
    name: str  # the user's words, as said ("work")
    slots: tuple[Slot, ...]
    at: float = 0.0

    def describe(self) -> str:
        """ "kitty and Zen Browser on 1, Visual Studio Code on 3" (by workspace)."""
        by_workspace: dict[str, list[str]] = {}
        for slot in self.slots:
            by_workspace.setdefault(slot.workspace, []).append(slot.app_name)
        parts = []
        for workspace in sorted(by_workspace, key=lambda w: (not w.isdigit(), int(w) if w.isdigit() else w)):
            names = by_workspace[workspace]
            apps = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
            parts.append(f"{apps} on {workspace}")
        return ", ".join(parts)


def key(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass
class Layouts:
    path: Path | None = None  # None: in memory only (tests)
    items: list[Layout] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Layouts:
        layouts = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            layouts.items = [
                Layout(
                    str(item["name"]),
                    tuple(
                        Slot(str(s["app"]), str(s["app_name"]), str(s["workspace"]), str(s.get("folder", "")))
                        for s in item["slots"]
                    )[:MAX_WINDOWS],
                    float(item.get("at", 0)),
                )
                for item in raw
            ][-MAX_LAYOUTS:]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            layouts.items = []
        return layouts

    def fresh(self) -> Layouts:
        """What's on disk now (sonud and the CLI both change it)."""
        return Layouts.load(self.path) if self.path is not None else self

    def get(self, name: str) -> Layout | None:
        return next((layout for layout in self.items if key(layout.name) == key(name)), None)

    def put(self, layout: Layout) -> None:
        """Save it, replacing one of the same name."""
        kept = [old for old in self.items if key(old.name) != key(layout.name)]
        self.items = [*kept, Layout(layout.name, layout.slots[:MAX_WINDOWS], layout.at or time.time())]
        self.items = self.items[-MAX_LAYOUTS:]
        self.save()

    def remove(self, name: str) -> bool:
        before = len(self.items)
        self.items = [layout for layout in self.items if key(layout.name) != key(name)]
        self.save()
        return len(self.items) < before

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(layout) for layout in self.items], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        tmp.replace(self.path)
