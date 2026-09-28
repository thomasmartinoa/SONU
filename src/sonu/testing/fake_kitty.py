"""Fake SONU-owned kitty terminals running a fake Claude Code, for tests and evals.

The screens copy what Claude Code 2.1.283 showed on the owner's laptop (see
``sonu.integrations.claude_code``): the folder-trust question, the header with the model, the input
line, "/model" opening the model menu ("s" = this session only, Enter = the default, Esc = keep).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROMPT = "\u276f"
MENU = ("Default (recommended)", "Opus 5.5", "Sonnet 5", "Fable 5.1", "Haiku 4.5", "Opus 5")


@dataclass
class FakeClaude:
    folder: str
    trusted: bool = False
    model: str = "Haiku 4.5"
    default: str = "Haiku 4.5"
    state: str = "trust"  # trust → ready ⇄ menu; closed
    trust_choice: int = 0  # 0: "No, exit", 1: "Yes, I trust this folder"
    menu_at: int = 4
    typed: str = ""
    pasted: str = ""  # a multi-line paste: shown as "[Pasted text #1 +N lines]", sent whole
    sent: list[str] = field(default_factory=list)  # what reached Claude Code (after Enter)
    lines: list[str] = field(default_factory=list)
    working: bool = False

    def __post_init__(self) -> None:
        if self.trusted:
            self.state = "ready"

    def screen(self) -> str:
        if self.state == "trust":
            no, yes = (f"{PROMPT} " if self.trust_choice == i else "  " for i in range(2))
            return (
                f" Accessing workspace:\n {self.folder}\n"
                " Quick safety check: Is this a project you created or one you trust?\n"
                " Claude Code'll be able to read, edit, and execute files here.\n"
                f" {no}No, exit\n {yes}Yes, I trust this folder\n"
                " Enter to confirm · Esc to cancel\n"
            )
        head = f" Claude Code v2.1.283\n {self.model} · Claude Pro\n {self.folder}\n"
        body = "".join(f"{line}\n" for line in self.lines)
        if self.state == "menu":
            rows = ""
            for i, label in enumerate(MENU):
                mark = PROMPT if i == self.menu_at else " "
                tick = " \u2714" if label == self.model else ""
                rows += f"   {mark} {i + 1}.  {label}{tick}  Good for things\n"
            menu_help = "   Enter to set as default · s to use this session only · Esc to cancel\n"
            return f"{head}{body}   Select model\n{rows}{menu_help}"
        status = "  esc to interrupt\n" if self.working else "  ⏸ manual mode on · ? for shortcuts\n"
        shown = f"[Pasted text #1 +{self.pasted.count(chr(10))} lines]" if self.pasted else self.typed
        return f"{head}{body}────\n{PROMPT} {shown}\n────\n{status}"

    def key(self, name: str) -> None:
        if self.state == "trust":
            if name in ("Down", "Up"):
                self.trust_choice = 1 - self.trust_choice
            elif name == "Enter":
                self.state = "ready" if self.trust_choice == 1 else "closed"
            elif name == "Escape":
                self.state = "closed"
        elif self.state == "menu":
            if name == "Down":
                self.menu_at = min(self.menu_at + 1, len(MENU) - 1)
            elif name == "Up":
                self.menu_at = max(self.menu_at - 1, 0)
            elif name in ("s", "Enter"):
                label = MENU[self.menu_at]
                chosen = self.default if label.startswith("Default") else label
                self.model = chosen
                if name == "Enter":
                    self.default = chosen
                    self.lines.append(
                        f"  ⎿  Set model to {chosen} and saved as your default for new sessions"
                    )
                else:
                    self.lines.append(f"  ⎿  Set model to {chosen} for this session only")
                self.state = "ready"
            elif name == "Escape":
                self.lines.append(f"  ⎿  Kept model as {self.model}")
                self.state = "ready"
        elif self.state == "ready" and name == "Enter":
            text, self.typed = (self.pasted or self.typed).strip(), ""
            shown = f"[Pasted text #1 +{self.pasted.count(chr(10))} lines]" if self.pasted else text
            self.pasted = ""
            self.lines.append(f"{PROMPT} {shown}")
            if text == "/model":
                self.state = "menu"
                self.menu_at = MENU.index(self.model) if self.model in MENU else 0
            elif text:
                self.sent.append(text)
                self.lines.append("● Working on it…")
                self.working = True

    def type(self, text: str) -> None:
        if self.state == "ready" and "\n" in text:
            self.pasted = text
        elif self.state == "ready":
            self.typed += text


class FakeKitty:
    """``kitty @ --to unix:<socket> …`` for the terminals launched through the fake desktop."""

    def __init__(self, trusted: set[str] | None = None) -> None:
        self.trusted = trusted or set()
        self.terminals: dict[str, FakeClaude] = {}  # socket → Claude Code in it

    def launched(self, argv: list[str]) -> None:
        """FakeHyprland started ``argv``: a SONU terminal if it listens on a socket."""
        if not argv or Path(argv[0]).name != "kitty" or "--listen-on" not in argv:
            return
        socket = argv[argv.index("--listen-on") + 1].removeprefix("unix:")
        folder = argv[argv.index("--directory") + 1] if "--directory" in argv else "~"
        if argv[-1] == "claude":
            self.terminals[socket] = FakeClaude(folder, trusted=folder in self.trusted)

    def claude(self, folder_name: str) -> FakeClaude | None:
        return next((c for c in self.terminals.values() if Path(c.folder).name == folder_name), None)

    def handle(self, args: list[str], stdin: bytes | None) -> tuple[int, str, str]:
        # args: ["@", "--to", "unix:<socket>", command, …]
        if len(args) < 4 or args[0] != "@" or args[1] != "--to":
            return 1, "", "unsupported kitty call"
        socket = args[2].removeprefix("unix:")
        claude = self.terminals.get(socket)
        if claude is None or claude.state == "closed":
            return 1, "", f"Error: Failed to connect to {socket}"
        command, rest = args[3], args[4:]
        if command == "ls":
            return 0, "[]", ""
        if command == "get-text":
            return 0, claude.screen(), ""
        if command == "send-text":
            claude.type((stdin or b"").decode() if "--stdin" in rest else rest[-1])
            return 0, "", ""
        if command == "send-key":
            for key in rest:
                claude.key(key)
            return 0, "", ""
        return 1, "", f"unknown command {command}"
