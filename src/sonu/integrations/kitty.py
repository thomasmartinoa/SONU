"""SONU-owned terminals: kitty windows SONU opened, driven over a private socket (D51).

kitty's remote control stays **off** for the user's own terminals. SONU starts each of its terminals
with remote control on for that window only (``allow_remote_control=socket-only``) and a socket in
``$XDG_RUNTIME_DIR/sonu/terminals`` (a 0700 folder), so it can type into — and read — exactly the
windows it opened, never whatever is focused. Each has a record (folder, program, title) next to its
socket; a terminal whose socket no longer answers is gone.

Text is sent through stdin (``send-text --stdin``): found in kitty's docs, a ``send-text`` argument
has Python escapes applied, so a prompt with a backslash would change; with ``--bracketed-paste`` a
multi-line prompt arrives as one paste instead of being sent line by line.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sonu.system.commands import CommandError, CommandRunner, ProgramMissing

TITLE_PREFIX = "SONU · "


@dataclass(frozen=True, slots=True)
class Terminal:
    id: str
    folder: str
    program: str
    title: str
    socket: str
    used: float  # when SONU last opened or typed into it

    @property
    def name(self) -> str:
        """How SONU talks about it: "Claude Code in shop-app"."""
        what = "Claude Code" if self.program == "claude" else self.program
        return f"{what} in {Path(self.folder).name}"


def runtime_folder(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    base = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "sonu" / "terminals"


class SonuTerminals:
    def __init__(self, folder: Path, runner: CommandRunner) -> None:
        self.folder, self.runner = folder, runner

    # ------------------------------------------------------------------ records
    def new(self, folder: Path, program: str) -> tuple[Terminal, list[str]]:
        """A new terminal's record and the argv that starts it (kitty running ``program`` in ``folder``)."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        ident = secrets.token_hex(4)
        socket = self.folder / f"kitty-{ident}.sock"
        title = f"{TITLE_PREFIX}{'Claude Code' if program == 'claude' else program} · {folder.name}"
        terminal = Terminal(ident, str(folder), program, title, str(socket), time.time())
        self._save(terminal)
        argv = [
            "kitty",
            "-o",
            "allow_remote_control=socket-only",
            "--listen-on",
            f"unix:{socket}",
            "--directory",
            str(folder),
            "--title",
            title,
            program,
        ]
        return terminal, argv

    def _save(self, terminal: Terminal) -> None:
        path = self.folder / f"{terminal.id}.json"
        path.write_text(json.dumps(asdict(terminal)))
        path.chmod(0o600)

    def touch(self, terminal: Terminal) -> None:
        self._save(Terminal(**{**asdict(terminal), "used": time.time()}))

    def forget(self, terminal: Terminal) -> None:
        (self.folder / f"{terminal.id}.json").unlink(missing_ok=True)

    def records(self) -> list[Terminal]:
        found: list[Terminal] = []
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                found.append(Terminal(**json.loads(path.read_text())))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(found, key=lambda t: t.used, reverse=True)  # most recently used first

    async def open_ones(self) -> list[Terminal]:
        """SONU's terminals that are still open (records of closed ones are dropped)."""
        alive: list[Terminal] = []
        for terminal in self.records():
            if await self.alive(terminal):
                alive.append(terminal)
            else:
                self.forget(terminal)
        return alive

    # ------------------------------------------------------------------ remote control
    async def _kitty(self, terminal: Terminal, *args: str, stdin: bytes | None = None) -> str:
        argv = ("kitty", "@", "--to", f"unix:{terminal.socket}", *args)
        try:
            result = await self.runner.run(argv, timeout_s=5.0, input_bytes=stdin)
        except ProgramMissing as exc:
            raise CommandError("kitty isn't installed") from exc
        if not result.ok:
            raise CommandError(result.problem())
        return result.stdout

    async def alive(self, terminal: Terminal) -> bool:
        try:
            await self._kitty(terminal, "ls")
        except CommandError:
            return False
        return True

    async def screen(self, terminal: Terminal) -> str:
        return await self._kitty(terminal, "get-text")

    async def type(self, terminal: Terminal, text: str) -> None:
        await self._kitty(terminal, "send-text", "--stdin", "--bracketed-paste=enable", stdin=text.encode())

    async def keys(self, terminal: Terminal, *keys: str) -> None:
        await self._kitty(terminal, "send-key", *keys)
