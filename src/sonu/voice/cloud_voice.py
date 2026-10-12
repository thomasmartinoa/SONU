"""SONU's voice from the cloud, opt-in (D98): Sarvam Bulbul (Indian English voices) or Google Gemini's speech
model, instead of the local Kokoro.

* Only what SONU says that holds nothing private: a reply carrying a private result (a research answer, your
  messages, a document's content) is always spoken by Kokoro on this laptop — the daemon decides that.
* Only with ``[privacy] cloud = "allow"`` and "speaking" in ``[cloud] use_for`` (asked first), and
  ``[voice] speaking`` set to the provider.
* Audited before it's sent — how many characters, never the text.
* If the cloud can't answer, Kokoro says it.

Both return 24 kHz audio, the rate Kokoro speaks at, so a reply can mix the two without a hitch.
"""

from __future__ import annotations

import array
import base64
import io
import logging
import os
import wave
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from sonu.audit.log import AuditError, AuditLog
from sonu.config.settings import Settings
from sonu.llm.cloud import cloud_allowed
from sonu.llm.providers import Provider, lookup_key

logger = logging.getLogger(__name__)

RATE = 24_000
SPEAKING = ("sarvam", "gemini")
DEFAULT_MODEL = {"sarvam": "bulbul:v3", "gemini": "gemini-2.5-flash-preview-tts"}
DEFAULT_VOICE = {"sarvam": "shubh", "gemini": "Kore"}
TIMEOUT_S = 20.0


class Voice(Protocol):
    rate: int

    def load(self) -> None: ...

    def speak(self, text: str) -> bytes: ...


class CloudVoiceError(Exception):
    pass


def float32_from_pcm16(pcm16: bytes) -> bytes:
    """16-bit samples → the float32 samples SONU's speaker plays."""
    samples = array.array("h", pcm16[: len(pcm16) - len(pcm16) % 2])
    return array.array("f", (s / 32768.0 for s in samples)).tobytes()


def pcm16_from_wav(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as file:
        if file.getsampwidth() != 2 or file.getnchannels() != 1:
            raise CloudVoiceError("the voice came back in a format SONU can't play")
        return file.readframes(file.getnframes()), file.getframerate()


class CloudVoice:
    """One provider's voice. ``transport``: tests pass a fake one."""

    def __init__(
        self,
        provider: Provider,
        key: str | None,
        voice: str = "",
        model: str = "",
        speed: float = 1.0,
        *,
        audit: AuditLog | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider, self.key, self.audit, self.speed = provider, key, audit, speed
        kind = provider.name if provider.name in SPEAKING else provider.kind
        if kind not in SPEAKING:
            raise ValueError(f"{provider.label} has no voice SONU can use")
        self.kind = kind
        self.model = model or DEFAULT_MODEL[kind]
        self.voice = voice or DEFAULT_VOICE[kind]
        self.rate = RATE
        self._client = httpx.Client(transport=transport, timeout=TIMEOUT_S)

    def load(self) -> None:
        """Nothing to load: it's a service."""

    def speak(self, text: str) -> bytes:
        if self.key is None:
            raise CloudVoiceError(
                f"no API key for {self.provider.label} — run `sonu settings key {self.provider.name}`"
            )
        self._record(len(text))
        pcm16 = self._sarvam(text) if self.kind == "sarvam" else self._gemini(text)
        return float32_from_pcm16(pcm16)

    def _record(self, chars: int) -> None:
        data = {"use": "speaking", "provider": self.provider.label, "model": self.model, "chars": chars}
        if self.audit is None:
            logger.info("cloud.send %s", data)
            return
        try:
            self.audit.append("cloud.send", data)
        except (OSError, AuditError) as exc:
            raise CloudVoiceError(f"couldn't write the audit log, so nothing was sent ({exc})") from exc

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise CloudVoiceError(f"{self.provider.label} didn't answer ({exc.__class__.__name__})") from exc
        if response.status_code in (401, 403):
            raise CloudVoiceError(
                f"{self.provider.label} refused the API key — `sonu settings key {self.provider.name}`"
            )
        if response.status_code >= 400:
            raise CloudVoiceError(
                f"{self.provider.label} answered {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CloudVoiceError(f"{self.provider.label} sent something that isn't JSON") from exc
        if not isinstance(data, dict):
            raise CloudVoiceError(f"{self.provider.label} sent an unexpected answer")
        return data

    def _sarvam(self, text: str) -> bytes:
        body = {
            "text": text[:2500],
            "language_code": "en-IN",
            "model": self.model,
            "speaker": self.voice,
            "pace": self.speed,
            "speech_sample_rate": RATE,
            "output_audio_codec": "wav",
        }
        data = self._post(
            f"{self.provider.base_url.rstrip('/')}/text-to-speech",
            body,
            {"api-subscription-key": str(self.key)},
        )
        try:
            pcm16, rate = pcm16_from_wav(base64.b64decode(data["audios"][0]))
        except (KeyError, IndexError, TypeError, ValueError, wave.Error) as exc:
            raise CloudVoiceError(f"{self.provider.label} sent no audio") from exc
        if rate != RATE:
            raise CloudVoiceError(f"{self.provider.label} spoke at {rate} Hz, not {RATE}")
        return pcm16

    def _gemini(self, text: str) -> bytes:
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}},
            },
        }
        data = self._post(
            f"{self.provider.base_url.rstrip('/')}/v1beta/models/{self.model}:generateContent",
            body,
            {"x-goog-api-key": str(self.key)},
        )
        try:
            inline = data["candidates"][0]["content"]["parts"][0]["inlineData"]
            return base64.b64decode(inline["data"])  # 24 kHz, mono, 16-bit
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CloudVoiceError(f"{self.provider.label} sent no audio") from exc


class FallbackVoice:
    """The cloud voice; Kokoro when the cloud can't answer — SONU never goes quiet because of it."""

    def __init__(self, cloud: CloudVoice, local: Voice) -> None:
        self.cloud, self.local = cloud, local
        self.rate = RATE

    def load(self) -> None:
        self.local.load()

    def speak(self, text: str) -> bytes:
        try:
            return self.cloud.speak(text)
        except CloudVoiceError as exc:
            logger.warning("cloud voice: %s — Kokoro says it", exc)
        return self.local.speak(text)


def cloud_voice(
    settings: Settings,
    local: Voice,
    audit: AuditLog | None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FallbackVoice | None:
    """The cloud voice when ``[voice] speaking`` names one and "speaking" is allowed; None: Kokoro."""
    chosen = settings.voice.speaking
    if chosen == "local" or not cloud_allowed(settings, "speaking"):
        return None
    from sonu.runtime import provider_for

    provider = provider_for(settings, chosen)
    key = lookup_key(provider, os.environ if env is None else env)
    voice = CloudVoice(
        provider,
        key,
        settings.voice.speaking_voice,
        settings.voice.speaking_model,
        settings.voice.speed,
        audit=audit,
        transport=transport,
    )
    return FallbackVoice(voice, local)
