"""How much goes to the cloud, in one click (D99): Fully local, Balanced, Full cloud — each a set of
catalogued settings written together (``change_many``), asked first when it gives the cloud more."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonu.config.settings import Settings

Mode = str  # "local", "balanced", "cloud" — or "custom" when the settings match none

ALL_USES = ["prompts", "planning", "vision", "reading", "replies", "hearing", "speaking"]
LABELS = {"local": "Fully local", "balanced": "Balanced", "cloud": "Full cloud", "custom": "Custom"}


def preset(name: str, has_key: Callable[[str], bool]) -> tuple[dict[str, Any], list[str]]:
    """The settings for a preset, and what couldn't be set (a provider without a key)."""
    if name == "local":
        return {
            "privacy.cloud": "never",
            "cloud.use_for": ["prompts", "planning"],
            "cloud.plan": "hard",
            "voice.hearing": "local",
            "voice.speaking": "local",
        }, []
    if name == "balanced":  # the cloud where it helps most; your voice stays here
        return {
            "privacy.cloud": "allow",
            "cloud.use_for": ["prompts", "planning", "reading", "vision"],
            "cloud.plan": "hard",
            "voice.hearing": "local",
            "voice.speaking": "local",
        }, []
    if name != "cloud":
        raise ValueError(f"no preset {name!r} (local, balanced, cloud)")
    notes: list[str] = []
    hearing = next((p for p in ("sarvam", "gemini", "groq") if has_key(p)), "local")
    speaking = next((p for p in ("sarvam", "gemini") if has_key(p)), "local")
    if hearing == "local":
        notes.append("hearing stays local — add a key: `sonu settings key sarvam`")
    if speaking == "local":
        notes.append("the voice stays local (Kokoro) — a Sarvam or Gemini key gives a cloud voice")
    return {
        "privacy.cloud": "allow",
        "cloud.use_for": list(ALL_USES),
        "cloud.plan": "always",
        "voice.hearing": hearing,
        "voice.speaking": speaking,
    }, notes


def current(settings: Settings) -> Mode:
    """Which preset the settings are, if any."""
    uses = set(settings.cloud.use_for)
    if settings.privacy.cloud != "allow":
        return "local" if settings.voice.hearing == settings.voice.speaking == "local" else "custom"
    if uses == {"prompts", "planning", "reading", "vision"} and settings.cloud.plan == "hard":
        return "balanced" if settings.voice.hearing == settings.voice.speaking == "local" else "custom"
    if uses == set(ALL_USES) and settings.cloud.plan == "always":
        return "cloud"
    return "custom"
