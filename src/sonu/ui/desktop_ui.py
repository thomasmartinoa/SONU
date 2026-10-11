"""Small desktop UI helpers: notifications and a text prompt / yes-no question via a dmenu-style picker.

Programs are always started with an argv list (never a shell) and a timeout. Anything shown
comes from SONU itself; user text is passed as data, never interpolated into commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

logger = logging.getLogger(__name__)

PICKER_TIMEOUT_S = 120.0
MAX_DETAILS = 3000  # what rofi's message area shows on a laptop screen
MAX_DETAIL_LINES = 45
NOTIFY_TIMEOUT_S = 5.0
MAX_NOTIFY_BODY = 600


async def _run(argv: Sequence[str], *, stdin: str | None, timeout_s: float) -> tuple[int, str] | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("could not start %s: %s", argv[0], exc)
        return None
    try:
        async with asyncio.timeout(timeout_s):
            out, _ = await process.communicate(stdin.encode() if stdin is not None else None)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return None
    return process.returncode or 0, out.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class Notifier:
    which: Callable[[str], str | None] = shutil.which

    async def send(self, title: str, body: str, *, urgent: bool = False) -> bool:
        if not self.which("notify-send"):
            return False
        text = body if len(body) <= MAX_NOTIFY_BODY else body[: MAX_NOTIFY_BODY - 1] + "…"
        argv = [
            "notify-send",
            "--app-name=SONU",
            f"--urgency={'critical' if urgent else 'normal'}",
            "--",  # end of options: text below can never be read as a flag
            title,
            text,
        ]
        result = await _run(argv, stdin=None, timeout_s=NOTIFY_TIMEOUT_S)
        return result is not None and result[0] == 0


PICKERS: dict[str, Callable[[str], list[str]]] = {
    "rofi": lambda prompt: ["rofi", "-dmenu", "-p", prompt, "-i", "-no-custom", "-format", "s"],
    "fuzzel": lambda prompt: ["fuzzel", "--dmenu", "--prompt", f"{prompt} "],
    "wofi": lambda prompt: ["wofi", "--dmenu", "--prompt", prompt],
}
TEXT_INPUT: dict[str, Callable[[str], list[str]]] = {
    "rofi": lambda prompt: ["rofi", "-dmenu", "-p", prompt, "-l", "0"],
    "fuzzel": lambda prompt: ["fuzzel", "--dmenu", "--prompt", f"{prompt} ", "--lines", "0"],
    "wofi": lambda prompt: ["wofi", "--dmenu", "--prompt", prompt, "--lines", "1"],
}


@dataclass(frozen=True, slots=True)
class Picker:
    """rofi, fuzzel or wofi in dmenu mode — whichever is installed first."""

    program: str

    @classmethod
    def detect(cls, which: Callable[[str], str | None] = shutil.which) -> Picker | None:
        for program in PICKERS:
            if which(program):
                return cls(program)
        return None

    async def ask_text(self, prompt: str) -> str | None:
        result = await _run(TEXT_INPUT[self.program](prompt), stdin="", timeout_s=PICKER_TIMEOUT_S)
        if result is None or result[0] != 0:
            return None
        text = result[1].strip()
        return text or None

    async def confirm(self, question: str, details: str | None = None) -> bool:
        """Yes/no with "No" first, so pressing Enter by accident never approves anything.

        ``details`` — what must be read before answering (a prompt SONU wrote, an outside tool's
        arguments) — are shown in full in rofi's message area. Other pickers can't show them, and
        too much doesn't fit: then the answer is no, never a yes to something unseen (D63).
        """
        prompt = question.replace("\n", " ")[:160]
        argv = self.choose_argv(prompt)
        if details:
            fits = len(details) <= MAX_DETAILS and details.count("\n") < MAX_DETAIL_LINES
            if self.program != "rofi" or not fits:
                logger.info("not asked on screen: the details don't fit in %s", self.program)
                return False
            argv = ["rofi", "-dmenu", "-p", "SONU", "-mesg", f"{escape(prompt)}\n\n{escape(details)}", "-i"]
            argv.append("-no-custom")
        result = await _run(argv, stdin="No\nYes\n", timeout_s=PICKER_TIMEOUT_S)
        return result is not None and result[0] == 0 and result[1].strip() == "Yes"

    async def choose(self, question: str, options: Sequence[str]) -> int | None:
        """Pick one option; returns its index, or None if the user cancelled.

        Lines are numbered ("1. kitty") and the answer is read back by number, so two options
        with the same text can't be confused and free text typed into the box is ignored.
        """
        prompt = question.replace("\n", " ")[:160]
        lines = [f"{number}. {' '.join(option.split())[:120]}" for number, option in enumerate(options, 1)]
        result = await _run(
            self.choose_argv(prompt), stdin="\n".join(lines) + "\n", timeout_s=PICKER_TIMEOUT_S
        )
        if result is None or result[0] != 0:
            return None
        head = result[1].strip().partition(".")[0]
        if head.isdigit() and 1 <= int(head) <= len(options):
            return int(head) - 1
        return None

    def choose_argv(self, prompt: str) -> list[str]:
        if self.program == "rofi":
            # rofi shows long text better in the message area than in the prompt.
            return ["rofi", "-dmenu", "-p", "SONU", "-mesg", prompt, "-i", "-no-custom"]
        return PICKERS[self.program](prompt)
