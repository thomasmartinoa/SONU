"""A stand-in for sonud's "Hey Sonu" switch (tests and evals never open the microphone)."""

from __future__ import annotations


class FakeWake:
    def __init__(self, listening: bool = False, problem: str | None = None) -> None:
        self._listening = listening
        self.problem = problem  # set: turning it on fails with this reason
        self.calls: list[bool] = []

    @property
    def listening(self) -> bool:
        return self._listening

    async def set(self, on: bool) -> str | None:
        self.calls.append(on)
        if on and self.problem:
            return self.problem
        self._listening = on
        return None
