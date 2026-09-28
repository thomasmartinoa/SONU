"""A stand-in for the local vision model, for evals (the fake desktop's screenshots aren't pictures)."""

from __future__ import annotations

from sonu.core.screen_reader import ScreenReader


class FakeScreenReader(ScreenReader):
    def __init__(
        self, answer: str = "A terminal with a Python error: ModuleNotFoundError: No module named 'gi'."
    ) -> None:
        self.answer, self.questions = answer, []  # type: ignore[var-annotated]

    async def read(self, png: bytes, question: str, what: str) -> str:
        self.questions.append((what, question))
        return self.answer
