"""SONU's voice from the cloud, opt-in (D98): what's sent, Kokoro as the fallback, private stays local."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from sonu.audit.log import AuditLog
from sonu.config.settings import CloudSettings, PrivacySettings, Settings, VoiceSettings
from sonu.core.planning import Plan
from sonu.core.report import StepRecord, TaskReport
from sonu.daemon.server import holds_private
from sonu.llm.providers import BUILT_IN
from sonu.tools.base import StepStatus, ToolOutcome
from sonu.voice.cloud_voice import CloudVoice, CloudVoiceError, FallbackVoice, cloud_voice
from sonu.voice.session import VoiceSession

PCM = b"\x00\x10" * 2400  # 0.1 s of 24 kHz 16-bit audio


def a_wav(rate: int = 24000) -> str:
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(PCM)
    return base64.b64encode(out.getvalue()).decode()


class Service:
    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.answers.pop(0)


class Kokoro:
    rate = 24000

    def __init__(self) -> None:
        self.said: list[str] = []

    def load(self) -> None: ...

    def speak(self, text: str) -> bytes:
        self.said.append(text)
        return b"\x00" * 8


def voice(name: str, service: Service, audit: AuditLog | None = None, key: str | None = "sk") -> CloudVoice:
    return CloudVoice(BUILT_IN[name], key, audit=audit, transport=httpx.MockTransport(service))


def test_sarvam_speaks_indian_english(tmp_path: Path) -> None:
    service = Service(httpx.Response(200, json={"audios": [a_wav()]}))
    audit = AuditLog(tmp_path / "audit.jsonl")
    samples = voice("sarvam", service, audit).speak("Kitty is up on workspace three.")
    assert len(samples) == len(PCM) * 2  # float32 for SONU's speaker
    sent = json.loads(service.seen[0].content)
    assert sent["language_code"] == "en-IN" and sent["speaker"] == "shubh" and sent["model"] == "bulbul:v3"
    assert sent["speech_sample_rate"] == 24000 and service.seen[0].headers["api-subscription-key"] == "sk"
    [send] = [r.data for r in audit.records() if r.kind == "cloud.send"]
    assert send == {
        "use": "speaking",
        "provider": "Sarvam",
        "model": "bulbul:v3",
        "chars": 31,
    }  # never the text
    with pytest.raises(CloudVoiceError, match="spoke at 22050"):
        voice("sarvam", Service(httpx.Response(200, json={"audios": [a_wav(22050)]}))).speak("hi")


def test_gemini_speaks_with_a_named_voice() -> None:
    answer = {
        "candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(PCM).decode()}}]}}]
    }
    service = Service(httpx.Response(200, json=answer))
    assert len(voice("gemini", service).speak("Hello.")) == len(PCM) * 2
    sent = json.loads(service.seen[0].content)
    assert sent["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert (
        sent["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    )


def test_kokoro_says_it_when_the_cloud_cant(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    kokoro = Kokoro()
    assert FallbackVoice(voice("sarvam", Service(), audit, key=None), kokoro).speak("Done.") == b"\x00" * 8
    assert kokoro.said == ["Done."] and [r for r in audit.records() if r.kind == "cloud.send"] == []
    down = FallbackVoice(voice("gemini", Service(httpx.Response(500, text="oops"))), kokoro)
    down.speak("Done again.")
    assert kokoro.said[-1] == "Done again."


def test_the_cloud_voice_only_when_chosen_and_allowed() -> None:
    chosen = Settings(voice=VoiceSettings(speaking="sarvam"))
    assert cloud_voice(chosen, Kokoro(), None, env={}) is None
    allowed = chosen.model_copy(
        update={"privacy": PrivacySettings(cloud="allow"), "cloud": CloudSettings(use_for=["speaking"])}
    )
    assert isinstance(cloud_voice(allowed, Kokoro(), None, env={}), FallbackVoice)


def test_a_private_reply_is_always_spoken_by_kokoro() -> None:
    kokoro = Kokoro()
    cloud = Kokoro()  # stands in for the cloud voice
    session = VoiceSession(None, kokoro, speaker_argv=("true",))  # type: ignore[arg-type]
    session.cloud_voice = cloud
    asyncio.run(session.say("Kitty is up."))
    asyncio.run(session.say("You have 3 unread messages from Alice.", local=True))
    assert cloud.said == ["Kitty is up."] and kokoro.said == ["You have 3 unread messages from Alice."]
    private = TaskReport("any messages?", Plan("steps", "", ()))
    private.steps = [
        StepRecord(0, "messages.unread", {}, "d", ToolOutcome(StepStatus.DONE, "3 from Alice", private=True))
    ]
    public = TaskReport("battery?", Plan("steps", "", ()))
    public.steps = [StepRecord(0, "system.status", {}, "d", ToolOutcome(StepStatus.DONE, "80%"))]
    assert holds_private(private) and not holds_private(public)
