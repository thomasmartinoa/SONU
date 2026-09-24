"""Best-effort removal of secrets from text before it is logged or audited.

This is a safety net, not a guarantee: SONU's first line of defence is never putting
secrets into logs, prompts or audit records in the first place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "«redacted»"

_KEY_NAME = r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd)"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Well-known provider key shapes (OpenAI/Anthropic/OpenRouter "sk-...", Groq "gsk_...", Google "AIza...").
    (re.compile(r"\b(?:sk|gsk|xai)[-_][A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),
    # HTTP bearer tokens.
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{12,}=*"), r"\1" + REDACTED),
    # "key": "quoted value, spaces included"
    (
        re.compile(rf"(?i)(\b{_KEY_NAME}[\"']?\s*[:=]\s*)([\"'])(?:(?!\2).)*\2"),
        r"\1\2" + REDACTED + r"\2",
    ),
    # key=value / key: value (unquoted)
    (
        re.compile(rf"(?i)(\b{_KEY_NAME}[\"']?\s*[:=]\s*)(?![\"'])[^\s,;}}]+"),
        r"\1" + REDACTED,
    ),
)

# Matches "token", "api_key", "openai_api_key", "github_token" — but not "max_tokens".
_SENSITIVE_KEY = re.compile(rf"(?:^|_|-){_KEY_NAME}$", re.IGNORECASE)


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside JSON-like data; values under sensitive keys are dropped."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): (REDACTED if _SENSITIVE_KEY.search(str(key)) else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(item) for item in value]
    return value
