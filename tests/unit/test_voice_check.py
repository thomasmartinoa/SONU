"""`sonu voice check`: how well each speech model hears the owner."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


def test_a_full_gpu_goes_on_with_the_cpu(
    xdg_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found on real hardware: after large-v3-turbo, large-v3 crashed with CUBLAS_STATUS_ALLOC_FAILED —
    sonud's own Whisper, Kokoro and planner share the 8 GB. That model goes on with the CPU instead."""
    import sonu.cli.voice_cmd as voice_cmd
    import sonu.voice.audio as audio
    import sonu.voice.engines as engines
    from sonu.cli.app import app

    devices: list[tuple[str, str]] = []

    class Ears:
        def __init__(self, name: str, device: str = "cuda") -> None:
            self.name, self.device = name, device
            devices.append((name, device))

        def load(self) -> None: ...

        def transcribe(self, audio: bytes, hint: str) -> str:
            if self.name == "large-v3" and self.device == "cuda":
                raise RuntimeError("cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED")
            return "Open kitty on workspace two."

    class Spoken:
        spoke = True

        def audio(self) -> bytes:
            return b"\x00" * 960

    async def record(*args: object, **kwargs: object) -> Spoken:
        return Spoken()

    monkeypatch.setattr(audio, "record", record)
    monkeypatch.setattr(engines, "WhisperEars", Ears)
    monkeypatch.setattr(engines, "silero_detector", lambda: None)
    monkeypatch.setattr(voice_cmd, "voice_installed", lambda: True)
    (tmp_path / "run").mkdir(exist_ok=True)
    env = xdg_env | {"XDG_RUNTIME_DIR": str(tmp_path / "run")}

    result = CliRunner().invoke(app, ["voice", "check", "-m", "large-v3-turbo", "-m", "large-v3"], env=env)
    assert result.exit_code == 0, result.output
    assert "the GPU is full" in result.output
    assert ("large-v3", "cpu") in devices
    assert result.output.count("of words right") == 2  # both models scored
