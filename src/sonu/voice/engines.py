"""Speech recognition (faster-whisper, on the GPU) and SONU's voice (Kokoro, on the CPU).

Both come with the optional ``voice`` extra and are imported only when used, so SONU without voice
never loads them. Measured on the owner's laptop (RTX 3070 Ti): Whisper large-v3-turbo transcribes a
4-second sentence in ~0.2 s; Kokoro speaks a 3.5-second sentence after ~0.8 s of work.

Two things found on the laptop:

* CTranslate2 (faster-whisper) is built against CUDA 12 and cuDNN 9; the system has CUDA 13 and no
  cuDNN. The ``nvidia-*-cu12`` packages carry those libraries; they're loaded (RTLD_GLOBAL) before
  CTranslate2 is imported, so it finds them without LD_LIBRARY_PATH.
* kokoro-onnx's bundled espeak data points at its build machine's path; SONU uses the system's
  ``espeak-ng`` instead (``pacman -S espeak-ng``).
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ESPEAK_LIB = Path("/usr/lib/libespeak-ng.so")
ESPEAK_DATA = Path("/usr/share/espeak-ng-data")


class VoiceUnavailable(Exception):
    """Voice can't be used: the extra isn't installed, a model is missing, or the GPU can't be used."""


def voice_installed() -> bool:
    return all(importlib.util.find_spec(name) for name in ("faster_whisper", "kokoro_onnx"))


def _preload_cuda_libraries() -> None:
    """Load the pip-provided CUDA 12 libraries so CTranslate2 finds them (see the module notes)."""
    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return
    for root in spec.submodule_search_locations:
        for package in ("cublas", "cudnn"):
            lib = Path(root) / package / "lib"
            for path in sorted(lib.glob("lib*.so*")) if lib.is_dir() else []:
                try:
                    ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    continue  # a library that needs another first; the one that matters loads


# ---------------------------------------------------------------------------- the voice's model files
@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size: int


KOKORO_FILES = (
    ModelFile(
        "kokoro-v1.0.onnx",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        325_532_387,
    ),
    ModelFile(
        "voices-v1.0.bin",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        28_214_398,
    ),
)


def missing_files(folder: Path) -> list[ModelFile]:
    return [
        f
        for f in KOKORO_FILES
        if not (folder / f.name).is_file() or (folder / f.name).stat().st_size != f.size
    ]


def download(file: ModelFile, folder: Path, client: httpx.Client | None = None) -> Path:
    """Download one model file, check its SHA-256 against the pinned one, then move it into place."""
    folder.mkdir(parents=True, exist_ok=True)
    target, partial = folder / file.name, folder / f"{file.name}.part"
    digest = hashlib.sha256()
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=60.0)
    try:
        with client.stream("GET", file.url) as response, partial.open("wb") as out:
            response.raise_for_status()
            for chunk in response.iter_bytes(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    finally:
        if own:
            client.close()
    if digest.hexdigest() != file.sha256:
        partial.unlink(missing_ok=True)
        raise VoiceUnavailable(f"{file.name} didn't match its checksum — not used")
    partial.replace(target)
    return target


# ---------------------------------------------------------------------------- hearing
def silero_detector() -> Callable[[bytes], list[tuple[int, int]]]:
    """Where the speech is in 16 kHz 16-bit audio (Silero VAD, bundled with faster-whisper)."""
    if not voice_installed():
        raise VoiceUnavailable("voice isn't installed")
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(min_silence_duration_ms=250, speech_pad_ms=60)

    def detect(pcm16: bytes) -> list[tuple[int, int]]:
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        return [(int(s["start"]), int(s["end"])) for s in get_speech_timestamps(audio, options)]

    return detect


# Whisper's own default; measured on the laptop's GPU: 0.21 s for a sentence, ~15 ms more than 1 —
# worth it after the owner's voice was misheard ("Hey Sonu" → "Hey Soin", "yes" → "Peace").
BEAM_SIZE = 5


class WhisperEars:
    """Speech → text. The model loads on first use and stays on the GPU."""

    def __init__(self, model: str = "large-v3-turbo", device: str = "cuda") -> None:
        self.model_name, self.device = model, device
        self._model: Any = None

    def load(self) -> None:
        if self._model is not None:
            return
        if not voice_installed():
            raise VoiceUnavailable("voice isn't installed (uv tool install … 'sonu[voice]')")
        _preload_cuda_libraries()
        from faster_whisper import WhisperModel

        compute = "int8_float16" if self.device == "cuda" else "int8"
        try:
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=compute)
        except (RuntimeError, ValueError, OSError) as exc:
            raise VoiceUnavailable(f"couldn't load Whisper {self.model_name}: {exc}") from exc

    def transcribe(self, pcm16: bytes, hint: str = "") -> str:
        """16 kHz mono 16-bit audio → text (English). ``hint``: words likely to be said (app names) —
        found on the laptop: "kitty" came out as "KIDDI" without one."""
        self.load()
        import numpy as np

        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            audio, language="en", beam_size=BEAM_SIZE, vad_filter=True, initial_prompt=hint or None
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


# ---------------------------------------------------------------------------- speaking
class ParakeetEars:
    """NVIDIA's Parakeet TDT 0.6B v3 (ONNX, int8, on the CPU — via ``onnx-asr``), for the hearing benchmark
    (phase 2). Small and fast; it takes no hint (names it doesn't know come out as they sound)."""

    name = "parakeet"

    def __init__(self, model: str = "nemo-parakeet-tdt-0.6b-v3") -> None:
        self.model_name = model
        self._model: Any = None

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import onnx_asr
        except ImportError as exc:
            raise VoiceUnavailable("onnx-asr isn't installed (it comes with `sonu[voice]`)") from exc
        try:
            self._model = onnx_asr.load_model(self.model_name, quantization="int8")
        except (RuntimeError, ValueError, OSError) as exc:
            raise VoiceUnavailable(f"couldn't load {self.model_name}: {exc}") from exc

    def transcribe(self, pcm16: bytes, hint: str = "") -> str:
        self.load()
        import numpy as np

        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        return str(self._model.recognize(audio, sample_rate=16_000) or "").strip()


class KokoroVoice:
    """Text → speech (24 kHz float32 samples)."""

    def __init__(self, folder: Path, voice: str = "af_heart", speed: float = 1.0) -> None:
        self.folder, self.voice, self.speed = folder, voice, speed
        self._tts: Any = None
        self.rate = 24_000

    def load(self) -> None:
        if self._tts is not None:
            return
        if not voice_installed():
            raise VoiceUnavailable("voice isn't installed (uv tool install … 'sonu[voice]')")
        if missing := missing_files(self.folder):
            raise VoiceUnavailable(
                f"the voice's model files aren't downloaded ({', '.join(f.name for f in missing)}) — "
                "run `sonu voice setup`"
            )
        if not (ESPEAK_LIB.exists() and ESPEAK_DATA.is_dir()):
            raise VoiceUnavailable("espeak-ng isn't installed (install: sudo pacman -S espeak-ng)")
        from kokoro_onnx import EspeakConfig, Kokoro

        self._tts = Kokoro(
            str(self.folder / "kokoro-v1.0.onnx"),
            str(self.folder / "voices-v1.0.bin"),
            espeak_config=EspeakConfig(lib_path=str(ESPEAK_LIB), data_path=str(ESPEAK_DATA)),
        )

    def speak(self, text: str) -> bytes:
        """Float32 mono samples at ``self.rate``, as bytes for the speaker."""
        self.load()
        samples, rate = self._tts.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        self.rate = int(rate)
        return bytes(samples.astype("float32").tobytes())


def voice_dir(data_dir: Path) -> Path:
    return data_dir / "voice"
