"""Hearing in the cloud, opt-in (D96): what each service is sent, the local fallback, what never leaves."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from sonu.audit.log import AuditLog
from sonu.config.settings import CloudSettings, PrivacySettings, Settings, VoiceSettings
from sonu.llm.providers import BUILT_IN
from sonu.voice.audio import Utterance
from sonu.voice.cloud_ears import CloudEars, CloudHearingError, FallbackEars, key_terms, request_ears
from sonu.voice.session import VoiceSession

HINT = "Hey Sonu. Yes. No. Workspace, kitty, Zen Browser, Chromium"
AUDIO = b"\x00\x01" * 16000  # one second of 16 kHz 16-bit audio


class Service:
    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.answers.pop(0)


class LocalEars:
    def __init__(self) -> None:
        self.heard: list[bytes] = []

    def load(self) -> None: ...

    def transcribe(self, pcm16: bytes, hint: str = "") -> str:
        self.heard.append(pcm16)
        return "heard on this laptop"


def ears(name: str, service: Service, audit: AuditLog | None = None, key: str | None = "sk") -> CloudEars:
    return CloudEars(BUILT_IN[name], key, audit=audit, transport=httpx.MockTransport(service))


def test_sarvam_hears_indian_english_without_key_terms(tmp_path: Any) -> None:
    """Measured on the owner's voice (`sonu voice bench`, D106): Sarvam heard names better without SONU's key
    terms (20 of 32 right, 17 with) — "btop" and "Zen" became "Vim" with them. Only the bench sends them."""
    service = Service(httpx.Response(200, json={"transcript": "open kitty on workspace 3"}))
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert ears("sarvam", service, audit).transcribe(AUDIO, HINT) == "open kitty on workspace 3"
    request = service.seen[0]
    assert (
        str(request.url) == "https://api.sarvam.ai/speech-to-text"
        and request.headers["api-subscription-key"] == "sk"
    )
    body = request.content
    assert b'name="model"\r\n\r\nsaaras:v4' in body and b'name="language_code"\r\n\r\nen-IN' in body
    assert b"RIFF" in body and b"WAVE" in body  # a WAV file
    assert b"keyterms" not in body and b"kitty" not in body
    [send] = [r.data for r in audit.records() if r.kind == "cloud.send"]
    assert send == {
        "use": "hearing",
        "provider": "Sarvam",
        "model": "saaras:v4",
        "audio_s": 1.0,
    }  # never the words

    measuring = Service(httpx.Response(200, json={"transcript": "open kitty"}))
    CloudEars(
        BUILT_IN["sarvam"], "sk", transport=httpx.MockTransport(measuring), sarvam_key_terms=True
    ).transcribe(AUDIO, HINT)
    assert json.dumps(key_terms(HINT)).encode() in measuring.seen[0].content  # the bench's "sarvam"


def test_groq_and_gemini_hear_too() -> None:
    groq = Service(httpx.Response(200, json={"text": "open zen"}))
    assert ears("groq", groq).transcribe(AUDIO, HINT) == "open zen"
    assert str(groq.seen[0].url).endswith("/openai/v1/audio/transcriptions")
    assert (
        b'name="model"\r\n\r\nwhisper-large-v3-turbo' in groq.seen[0].content
        and b"kitty" in groq.seen[0].content
    )
    gemini = Service(
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "open zen"}]}}]})
    )
    assert ears("gemini", gemini).transcribe(AUDIO) == "open zen"
    sent = json.loads(gemini.seen[0].content)
    assert sent["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "audio/wav"


def test_gemini_transcribe_hears_with_the_names_as_vocabulary() -> None:
    """Gemini 3.5 Transcribe (phase 2): its own API, the expected names as custom vocabulary — checked on the
    laptop with a Kokoro clip: "install cowsay" was "Kausay" without them, "cowsay" with."""
    answer = {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "Install cowsay."}]}],
    }
    service = Service(httpx.Response(200, json=answer))
    hearing = CloudEars(
        BUILT_IN["gemini"], "sk", "gemini-3.5-transcribe", transport=httpx.MockTransport(service)
    )
    assert hearing.transcribe(AUDIO, HINT + ", cowsay") == "Install cowsay."
    assert str(service.seen[0].url).endswith("/v1beta/interactions")
    sent = json.loads(service.seen[0].content)
    assert sent["model"] == "gemini-3.5-transcribe"
    assert sent["input"][0]["type"] == "audio" and sent["input"][0]["mime_type"] == "audio/wav"
    config = sent["generation_config"]["transcription_config"]
    assert "cowsay" in config["custom_vocabulary"] and "kitty" in config["custom_vocabulary"]
    assert service.seen[0].headers["x-goog-api-key"] == "sk"


def test_a_used_up_limit_is_said_on_one_line_with_which_limit() -> None:
    """Found running the bench: Gemini's 429 was printed as 200 characters of JSON over five lines, cut
    before it said the limit was 20 requests a day."""
    body = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota, please check your plan and billing details. "
            "For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}
                    ],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "76533s"},
            ],
        }
    }
    with pytest.raises(CloudHearingError) as raised:
        ears("gemini", Service(httpx.Response(429, json=body))).transcribe(AUDIO)
    said = str(raised.value)
    assert "\n" not in said and said.startswith("Google Gemini answered 429: You exceeded your current quota")
    assert "GenerateRequestsPerDayPerProjectPerModel-FreeTier: 20" in said and "retry in 76533s" in said
    with pytest.raises(CloudHearingError, match="answered 500: oops it broke"):
        ears("gemini", Service(httpx.Response(500, text="oops\n  it broke"))).transcribe(AUDIO)


def test_the_local_whisper_hears_it_when_the_cloud_cant(tmp_path: Any) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    local = LocalEars()
    no_key = FallbackEars(ears("sarvam", Service(), audit, key=None), local)
    assert no_key.transcribe(AUDIO, HINT) == "heard on this laptop"
    assert [
        r for r in audit.records() if r.kind == "cloud.send"
    ] == []  # nothing sent, nothing logged as sent
    down = FallbackEars(ears("groq", Service(httpx.Response(503, text="busy"))), local)
    assert down.transcribe(AUDIO) == "heard on this laptop"
    with pytest.raises(CloudHearingError, match="refused the API key"):
        ears("sarvam", Service(httpx.Response(401))).transcribe(AUDIO)


def test_cloud_hearing_only_when_chosen_and_allowed() -> None:
    local = LocalEars()
    chosen = Settings(voice=VoiceSettings(hearing="sarvam"))
    assert request_ears(chosen, local, None, env={}) is None  # cloud not allowed
    allowed = chosen.model_copy(
        update={"privacy": PrivacySettings(cloud="allow"), "cloud": CloudSettings(use_for=["hearing"])}
    )
    assert isinstance(request_ears(allowed, local, None, env={}), FallbackEars)
    with pytest.raises(ValueError, match="Sarvam is for hearing"):
        Settings(cloud=CloudSettings(provider="sarvam"))


def test_a_spoken_password_never_goes_to_the_cloud() -> None:
    local = LocalEars()
    cloud_heard: list[bytes] = []

    class Cloud:
        def load(self) -> None: ...

        def transcribe(self, pcm16: bytes, hint: str = "") -> str:
            cloud_heard.append(pcm16)
            return "open kitty"

    async def recorder(endpointing: Any, detector: Any) -> Utterance:
        utterance = Utterance()
        utterance.spoke = True
        return utterance

    class Voice:
        rate = 24000

        def speak(self, text: str) -> bytes:
            return b""

    session = VoiceSession(local, Voice(), recorder=recorder, speaker_argv=("true",))  # type: ignore[arg-type]
    session.request_ears = Cloud()
    assert asyncio.run(session.hear()) == "open kitty"  # a request: the cloud hears it
    assert asyncio.run(session.hear_secret("Say your password.")) == "heardonthislaptop"  # a password: never
    assert len(cloud_heard) == 1 and len(local.heard) == 1
