"""Reading the user's own documents (D82): "summarise my invoice pdf", "what does notes.txt say about
parking?", "read my resume".

SONU finds the file from the user's words (like opening one), takes its text — a text file as it is, a PDF
with ``pdftotext``, a Word or OpenDocument file with LibreOffice under SONU's own profile — and the planner
model **on this laptop** answers the question from it (``core/page_reader.py``: cloud models and remote
Ollama are refused, the text is content, never instructions). The answer is private: the document is the
user's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.page_reader import PageReaderError
from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import OFFICE_SUFFIXES, TEXT_SUFFIXES
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.file_change_tools import SOURCE, _ask, find_sources, protected
from sonu.tools.file_tools import home_relative
from sonu.tools.grounding import not_own_words
from sonu.tools.history_tools import copies_folder
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_FILE = 50_000_000  # bytes: bigger than this isn't a document to read out


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=160, description="the user's words for the file ('invoice pdf')"
    )
    question: str = Field(
        default="Summarise it.", max_length=300, description="the user's question about it; a summary if none"
    )


def _what_reads(path: Path) -> str | None:
    """The program that turns this kind of file into text (None: read as it is), or "" when SONU can't."""
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return None
    if suffix == ".pdf":
        return "pdftotext"
    if suffix in OFFICE_SUFFIXES:
        return "soffice"
    return ""


class ReadDocument(Tool):
    name = "files.read"
    description = (
        "Summarise or answer a question about one of the user's documents (a text file, PDF, Word or "
        "OpenDocument file) with the local model: 'summarise my invoice pdf', 'what does notes.txt say "
        "about X?'."
    )
    risk = Risk.READ_ONLY
    effects = frozenset({Effect.FILE_READ})
    Args = ReadArgs
    grounded = ("name",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ReadArgs, args)
        if problem := not_own_words(ctx, a.name):
            return problem
        if ctx.pages is None:
            return ToolOutcome(StepStatus.FAILED, "There's no local model to read it with")
        home = ctx.system.home
        if choice and choice.startswith(SOURCE):
            found = [Path(choice.removeprefix(SOURCE))]
        else:
            found = await asyncio.to_thread(
                find_sources, a.name, ctx.request_text, home, ctx.system.screenshot_dir()
            )
            found = [path for path in found if path.is_file()]
        if not found:
            return ToolOutcome(StepStatus.FAILED, f"There's no file called '{a.name}' in your home")
        if len(found) > 1:
            return _ask(f"'{a.name}'", found, home, SOURCE)
        path = found[0]
        if (why := protected(path, home)) and why.startswith("hidden"):
            return ToolOutcome(StepStatus.FAILED, f"SONU won't read '{path.name}': {why}")
        program = _what_reads(path)
        if program == "":
            return ToolOutcome(
                StepStatus.FAILED,
                f"SONU reads text files, PDFs and Word / OpenDocument files — not '{path.name}'",
            )
        if program and not ctx.system.available(program):
            package = "poppler" if program == "pdftotext" else "libreoffice-fresh"
            return ToolOutcome(StepStatus.FAILED, f"{program} isn't installed (sudo pacman -S {package})")
        if path.stat().st_size > MAX_FILE:
            return ToolOutcome(StepStatus.FAILED, f"'{path.name}' is too big to read")
        label = f"'{path.name}' ({home_relative(path.parent, home)})"
        return Prepared(f"Read {label} with the local model", (path, a.question, label))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        path, question, label = cast("tuple[Path, str, str]", prepared.payload)
        reader = ctx.pages
        if reader is None:
            return ToolOutcome(StepStatus.FAILED, "There's no local model to read it with")
        try:
            reader.check_where()
            text = await ctx.system.document_text(path, copies_folder().parent / "documents")
        except PageReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read it: {exc}")
        except (CommandError, ProgramMissing, OSError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't get the text of '{path.name}': {exc}")
        if not text or not text.strip():
            return ToolOutcome(
                StepStatus.FAILED, f"'{path.name}' has no text SONU can read (a scanned page is a picture)"
            )
        part = text[: reader.max_text]
        partial = len(part) < len(text)
        try:
            answer = await reader.read(part, question, partial=partial, what=f"the document '{path.name}'")
        except PageReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read it: {exc}")
        if partial:
            answer += " (SONU read the start of a long document.)"
        # The user's document, through the local model: for their eyes and ears only.
        return ToolOutcome(
            StepStatus.DONE, answer, (Check(f"the local model read {label}", True),), private=True
        )


DOCUMENT_TOOLS: tuple[Tool, ...] = (ReadDocument(),)
