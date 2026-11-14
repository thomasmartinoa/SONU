"""Hearing in the cloud, opt-in (D96): Sarvam (built for Indian English and Hinglish, with key terms), Groq
(Whisper large-v3, hosted) or Google Gemini (listens to the audio) — for the request the user speaks after
the SONU key or "Hey Sonu", and nothing else.

* Only the request goes: listening for "Hey Sonu", noticing the user talk over SONU, and a password said for
  an install always stay with the local Whisper — the microphone is never streamed anywhere.
* Only with ``[privacy] cloud = "allow"`` and "hearing" in ``[cloud] use_for`` (asked first), and ``[voice]
  hearing`` set to the provider.
* Audited before it's sent — how many seconds of audio, never the audio or the words.
* If the cloud can't answer (no key, offline, a limit), the local Whisper hears it instead.
* The app names SONU expects (its speech hint) go along as key terms (Sarvam) or a prompt (Groq): found on
  the laptop, "kitty on" was heard as "Kitchen" and "browser" as "boot".
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import wave
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from sonu.audit.log import AuditError, AuditLog
from sonu.config.settings import Settings
from sonu.llm.cloud import cloud_allowed
from sonu.llm.providers import Provider, lookup_key

logger = logging.getLogger(__name__)

RATE = 16_000
HEARING = ("sarvam", "groq", "gemini")  # providers that can hear
DEFAULT_MODEL = {"sarvam": "saaras:v4", "groq": "whisper-large-v3-turbo", "gemini": "gemini-flash-latest"}
MAX_TERMS = 50  # Sarvam's limit for key terms
MAX_VOCABULARY = 100  # Gemini 3.5 Transcribe: "about 100 gives the best results"
TIMEOUT_S = 20.0


class Ears(Protocol):
    def load(self) -> None: ...

    def transcribe(self, pcm16: bytes, hint: str = "") -> str: ...


class CloudHearingError(Exception):
    pass


def _reason(response: httpx.Response) -> str:
    """The service's reason on one line — for a used-up limit, which limit and when to retry (found running
    the bench: Gemini's free tier allows 20 requests a day per model, and the error was cut before saying so).
    """
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if not isinstance(error, dict) or not error.get("message"):
        return " ".join(response.text.split())[:200]
    said = [" ".join(str(error["message"]).split()).split(". ")[0][:160]]
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict) and violation.get("quotaId"):
                said.append(f"[{violation['quotaId']}: {violation.get('quotaValue', '?')}]")
        if detail.get("retryDelay"):
            said.append(f"retry in {detail['retryDelay']}")
    return " ".join(said)


def wav(pcm16: bytes) -> bytes:
    """16 kHz mono 16-bit audio as a WAV file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(pcm16)
    return out.getvalue()


def key_terms(hint: str) -> list[str]:
    """SONU's speech hint ("Hey Sonu. Yes. No. Workspace, kitty, Zen Browser, …") as separate terms."""
    terms = [term.strip() for term in re.split(r"[.,]", hint) if term.strip()]
    return [term[:64] for term in dict.fromkeys(terms)][:MAX_TERMS]


class CloudEars:
    """One provider's speech recognition. ``transport``: tests pass a fake one."""

    def __init__(
        self,
        provider: Provider,
        key: str | None,
        model: str = "",
        *,
        audit: AuditLog | None = None,
        transport: httpx.BaseTransport | None = None,
        sarvam_key_terms: bool = False,
    ) -> None:
        if provider.kind not in HEARING and provider.name not in HEARING:
            raise ValueError(f"{provider.label} can't hear")
        self.provider, self.key, self.audit = provider, key, audit
        self.model = model or DEFAULT_MODEL.get(provider.name, DEFAULT_MODEL.get(provider.kind, ""))
        self.name = f"{provider.label} ({self.model})"
        self.sarvam_key_terms = sarvam_key_terms
        self._client = httpx.Client(transport=transport, timeout=TIMEOUT_S)

    def load(self) -> None:
        """Nothing to load: it's a service."""

    def transcribe(self, pcm16: bytes, hint: str = "") -> str:
        if self.key is None:
            raise CloudHearingError(
                f"no API key for {self.provider.label} — run `sonu settings key {self.provider.name}`"
            )
        seconds = round(len(pcm16) / (RATE * 2), 1)
        self._record(seconds)
        audio = wav(pcm16)
        kind = self.provider.name if self.provider.name in HEARING else self.provider.kind
        if kind == "sarvam":
            return self._sarvam(audio, hint)
        if kind == "gemini":
            return self._gemini(audio, hint)
        return self._whisper_api(audio, hint)

    def _record(self, seconds: float) -> None:
        data = {"use": "hearing", "provider": self.provider.label, "model": self.model, "audio_s": seconds}
        if self.audit is None:
            logger.info("cloud.send %s", data)
            return
        try:
            self.audit.append("cloud.send", data)
        except (OSError, AuditError) as exc:
            raise CloudHearingError(f"couldn't write the audit log, so nothing was sent ({exc})") from exc

    def _post(self, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudHearingError(
                f"{self.provider.label} didn't answer ({exc.__class__.__name__})"
            ) from exc
        if response.status_code in (401, 403):
            raise CloudHearingError(
                f"{self.provider.label} refused the API key — `sonu settings key {self.provider.name}`"
            )
        if response.status_code >= 400:
            raise CloudHearingError(
                f"{self.provider.label} answered {response.status_code}: {_reason(response)}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CloudHearingError(f"{self.provider.label} sent something that isn't JSON") from exc
        if not isinstance(data, dict):
            raise CloudHearingError(f"{self.provider.label} sent an unexpected answer")
        return data

    def _sarvam(self, audio: bytes, hint: str) -> str:
        fields: dict[str, str] = {"model": self.model, "language_code": "en-IN"}
        # Key terms (the app names SONU expects) made names worse on the owner's voice: 17 of 32 right with
        # them, 20 without (`sonu voice bench`, D106) — only the bench still sends them, to measure
        if hint and self.sarvam_key_terms and self.model.endswith("v4"):
            fields["keyterms"] = json.dumps(key_terms(hint))
        data = self._post(
            f"{self.provider.base_url.rstrip('/')}/speech-to-text",
            headers={"api-subscription-key": str(self.key)},
            data=fields,
            files={"file": ("speech.wav", audio, "audio/wav")},
        )
        return str(data.get("transcript") or "").strip()

    def _whisper_api(self, audio: bytes, hint: str) -> str:
        """OpenAI's transcription API, as Groq serves it (Whisper large-v3)."""
        fields: dict[str, str] = {"model": self.model, "language": "en", "response_format": "json"}
        if hint:
            fields["prompt"] = hint[:800]
        data = self._post(
            f"{self.provider.base_url.rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.key}"},
            data=fields,
            files={"file": ("speech.wav", audio, "audio/wav")},
        )
        return str(data.get("text") or "").strip()

    def _gemini(self, audio: bytes, hint: str) -> str:
        if "transcribe" in self.model:  # Gemini 3.5 Transcribe: a speech-to-text model, not a chat model
            return self._gemini_transcribe(audio, hint)
        words = f" Words that may be said: {hint}." if hint else ""
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"Write down exactly what is said, in English, and nothing else.{words}"},
                        {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(audio).decode()}},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
        }
        data = self._post(
            f"{self.provider.base_url.rstrip('/')}/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": str(self.key)},
            json=body,
        )
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudHearingError(f"{self.provider.label} heard nothing") from exc
        return "".join(str(p.get("text") or "") for p in parts).strip()

    def _gemini_transcribe(self, audio: bytes, hint: str) -> str:
        """Gemini 3.5 Transcribe (Interactions API) with the expected names as its custom vocabulary — checked
        on the laptop (a Kokoro clip): "install cowsay" came out as "Kausay" without, "cowsay" with."""
        config: dict[str, Any] = {"language_codes": ["en-IN"]}
        if hint:
            config["custom_vocabulary"] = key_terms(hint)[:MAX_VOCABULARY]
        body = {
            "model": self.model,
            "input": [{"type": "audio", "data": base64.b64encode(audio).decode(), "mime_type": "audio/wav"}],
            "generation_config": {"transcription_config": config},
        }
        data = self._post(
            f"{self.provider.base_url.rstrip('/')}/v1beta/interactions",
            headers={"x-goog-api-key": str(self.key)},
            json=body,
        )
        try:
            steps = data["steps"]
        except (KeyError, TypeError) as exc:
            raise CloudHearingError(f"{self.provider.label} heard nothing") from exc
        texts = [
            str(part.get("text") or "")
            for step in steps
            if isinstance(step, dict) and step.get("type") == "model_output"
            for part in step.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(texts).strip()


class FallbackEars:
    """The cloud first; the local Whisper when the cloud can't answer — a request is never lost to it."""

    def __init__(self, cloud: CloudEars, local: Ears) -> None:
        self.cloud, self.local = cloud, local
        self.name = cloud.name

    def load(self) -> None:
        self.local.load()  # ready for when the cloud is away

    def transcribe(self, pcm16: bytes, hint: str = "") -> str:
        try:
            return self.cloud.transcribe(pcm16, hint)
        except CloudHearingError as exc:
            logger.warning("cloud hearing: %s — using the local model", exc)
        return self.local.transcribe(pcm16, hint)


def request_ears(
    settings: Settings,
    local: Ears,
    audit: AuditLog | None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FallbackEars | None:
    """Who hears the user's requests: a cloud service when ``[voice] hearing`` names one and the user allowed
    "hearing"; None (the local ears) otherwise."""
    chosen = settings.voice.hearing
    if chosen == "local" or not cloud_allowed(settings, "hearing"):
        return None
    from sonu.runtime import provider_for  # the providers the user set up (and the built-in ones)

    provider = provider_for(settings, chosen)
    key = lookup_key(provider, os.environ if env is None else env)
    cloud = CloudEars(provider, key, settings.voice.hearing_model, audit=audit, transport=transport)
    return FallbackEars(cloud, local)


SECOND_OPINION_MODEL = "gemini-3.5-transcribe"


def second_opinion_ears(
    settings: Settings,
    audit: AuditLog | None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> CloudEars | None:
    """Who listens again when a name matched nothing (D111): Gemini 3.5 Transcribe, when the user turned the
    second opinion on, allowed cloud "hearing", and has a Gemini key. None otherwise — and nothing is sent."""
    if not settings.voice.second_opinion or not cloud_allowed(settings, "hearing"):
        return None
    from sonu.runtime import provider_for

    provider = provider_for(settings, "gemini")
    key = lookup_key(provider, os.environ if env is None else env)
    if key is None:
        return None
    return CloudEars(provider, key, SECOND_OPINION_MODEL, audit=audit, transport=transport)
