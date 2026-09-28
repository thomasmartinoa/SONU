"""Reading Claude Code's screen (in a SONU-owned terminal) — what state it's in, which model it uses.

Everything here was read from Claude Code 2.1.283 on the owner's laptop (2026-09-28):

* a folder it doesn't trust yet: "Quick safety check: Is this a project you created or one you
  trust? … No, exit / Yes, I trust this folder … Enter to confirm · Esc to cancel" (Down, Enter
  answers yes);
* ready: a header "Claude Code v2.1.283" / "Haiku 4.5 · Claude Pro" / the folder, and an input line
  starting with ``PROMPT``;
* "/model sonnet" + Enter → "⎿  Set model to Sonnet 5 and saved as your default for new sessions",
  and the header shows "Sonnet 5 · Claude Pro";
* "/model" alone opens a menu: "Select model … 5.  Haiku 4.5 ✔ … Enter to set as default · s to use
  this session only · Esc to cancel"; typing "/model sonnet" fast can land there too (Enter picks the
  suggestion "/model"), so SONU uses the menu on purpose and presses "s": only that window changes;
* a multi-line paste shows in the input box as "[Pasted text #1 +4 lines]";
* whether a folder is trusted is in ``~/.claude.json``: ``projects[<folder>].hasTrustDialogAccepted``.

The screen is the user's and Claude's text — data, never instructions for SONU's model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PROMPT = "\u276f"  # the input line's mark in Claude Code
_MODEL = re.compile(r"\b(Haiku|Sonnet|Opus) [0-9][0-9.]*\b")
_ROW = re.compile(r"^\s*(\u276f)?\s*(\d+)\.\s+(.+?)(?:\s{2,}|$)")
_SET_MODEL = re.compile(r"Set model to ((?:Haiku|Sonnet|Opus)[^\n]*?)(?: and saved|\s*$)", re.M)
MODELS = ("sonnet", "opus", "haiku")


def compact(text: str) -> str:
    """Letters and digits only, lowercase — to find typed text however the screen wraps it."""
    return "".join(c for c in text.lower() if c.isalnum())


@dataclass(frozen=True, slots=True)
class ClaudeScreen:
    text: str

    @property
    def asks_trust(self) -> bool:
        return "trust this folder" in self.text and "Enter to confirm" in self.text

    @property
    def ready(self) -> bool:
        return "Claude Code v" in self.text and PROMPT in self.text and not self.asks_trust

    @property
    def working(self) -> bool:
        return "esc to interrupt" in self.text.lower()

    @property
    def asks_permission(self) -> str | None:
        """Claude Code asking the user something ("Do you want to create index.html?")."""
        for line in self.text.splitlines():
            if (found := line.strip()).startswith("Do you want to"):
                return found
        return None

    @property
    def model(self) -> str | None:
        """The model in the header ("Sonnet 5")."""
        head = "\n".join(self.text.splitlines()[:6])
        match = _MODEL.search(head)
        return match.group(0) if match else None

    def model_set(self) -> list[str]:
        """Every "Set model to …" line on the screen, oldest first."""
        return [m.group(1).strip() for m in _SET_MODEL.finditer(self.text)]

    @property
    def model_menu(self) -> bool:
        return "Select model" in self.text and "Esc to cancel" in self.text

    def menu_rows(self) -> list[tuple[str, bool]]:
        """The model menu's rows, in order: (label, selected)."""
        rows: list[tuple[str, bool]] = []
        for line in self.text.splitlines():
            if match := _ROW.match(line.replace("\u2191", " ").replace("\u2193", " ")):
                rows.append((match.group(3).replace("\u2714", "").strip(), bool(match.group(1))))
        return rows

    def input_line(self) -> str:
        """The text in the input box (the last line starting with ``PROMPT``)."""
        lines = [line.strip() for line in self.text.splitlines() if line.strip().startswith(PROMPT)]
        return lines[-1].removeprefix(PROMPT).strip() if lines else ""

    @property
    def pasted(self) -> bool:
        """The input box holds a multi-line paste ("[Pasted text #1 +4 lines]")."""
        return self.input_line().startswith("[Pasted text")

    def holds(self, typed: str) -> bool:
        """``typed`` is in the input box, waiting for Enter."""
        return self.pasted or ClaudeScreen(self.input_line()).shows(typed)

    def shows(self, typed: str) -> bool:
        """``typed`` (or its first 40 letters) is on the screen, however it's wrapped."""
        wanted = compact(typed)[:40]
        return bool(wanted) and wanted in compact(self.text)


def trusted(folder: Path, home: Path) -> bool | None:
    """Did the user tell Claude Code to trust ``folder``? None: can't tell (no ~/.claude.json)."""
    try:
        config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    project = config.get("projects", {}).get(str(folder), {}) if isinstance(config, dict) else {}
    return bool(isinstance(project, dict) and project.get("hasTrustDialogAccepted"))
