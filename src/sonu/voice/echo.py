"""SONU's own echo canceller, so the user can talk over it (D50).

A laptop's microphone hears its speakers: while SONU talks, its own voice looks like speech.
PipeWire's echo-cancel module (WebRTC) takes what SONU plays as the reference and removes it from
the microphone. SONU adds one for itself — a sink it plays its voice to and a source it listens on —
and leaves the user's default speaker and microphone alone. It lives as long as ``sonud``.

Measured on the laptop (built-in speaker at 100 %, built-in mic): SONU's voice alone gave the plain
mic an RMS of 2467 and speech the whole time; through the canceller, 214 and one 0.38 s blip that
transcribes to nothing. Another voice over it was found at once.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

SINK = "sonu_aec_sink"
SOURCE = "sonu_aec_source"
_LOAD = (
    "pactl",
    "load-module",
    "module-echo-cancel",
    "aec_method=webrtc",
    f"sink_name={SINK}",
    f"source_name={SOURCE}",
    'sink_properties=device.description="SONU (echo cancel)"',
    'source_properties=device.description="SONU (echo cancel)"',
)

Run = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout


@dataclass
class EchoCanceller:
    run: Run = _run
    which: Callable[[str], str | None] = shutil.which
    module: str | None = None  # the module SONU loaded (unloaded again by close())

    def ensure(self) -> bool:
        """The canceller exists (reused if a previous sonud left it); False if it can't be made."""
        if not self.which("pactl"):
            return False
        code, sinks = self.run(["pactl", "list", "short", "sinks"])
        if code == 0 and any(line.split("\t")[1:2] == [SINK] for line in sinks.splitlines()):
            return True
        code, out = self.run(list(_LOAD))
        if code != 0 or not out.strip().isdigit():
            return False
        self.module = out.strip()
        return True

    def close(self) -> None:
        if self.module is not None:
            self.run(["pactl", "unload-module", self.module])
            self.module = None
