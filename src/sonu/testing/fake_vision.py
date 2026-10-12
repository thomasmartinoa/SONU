"""Stand-ins for the local models that read the screen and pages, for evals (the fake desktop's
screenshots aren't pictures)."""

from __future__ import annotations

from sonu.core.page_reader import MAX_TEXT, PageReader
from sonu.core.screen_reader import ScreenReader


class FakeScreenReader(ScreenReader):
    def __init__(
        self, answer: str = "A terminal with a Python error: ModuleNotFoundError: No module named 'gi'."
    ) -> None:
        self.answer, self.questions = answer, []  # type: ignore[var-annotated]

    async def read(self, png: bytes, question: str, what: str) -> str:
        self.questions.append((what, question))
        return self.answer


class FakePageReader(PageReader):
    """A stand-in for the local model reading a page (D71): records what it was given."""

    def __init__(self, answer: str = "The page is about SONU, a local voice assistant for Linux.") -> None:
        self.answer, self.max_text, self.host = answer, MAX_TEXT, "http://127.0.0.1:11434"
        self.calls: list[tuple[str, str, bool, bool]] = []
        self.whats: list[str] = []  # what each text was ("the document 'notes.txt'"; "" for a page)

    def check_where(self) -> None:
        return None

    async def read(
        self, text: str, question: str, *, partial: bool = False, selection: bool = False, what: str = ""
    ) -> str:
        self.calls.append((text, question, partial, selection))
        self.whats.append(what)
        return self.answer
