from __future__ import annotations

from sonu.redact import REDACTED, redact_text, redact_value


def test_provider_keys_are_redacted() -> None:
    text = "using sk-ant-api03-abcdefghijklmnopqrstuvwxyz and gsk_ABCDEFGHIJKLMNOPQRSTUV"
    result = redact_text(text)
    assert "abcdefghijklmnop" not in result
    assert "ABCDEFGHIJKLMNOP" not in result
    assert result.count(REDACTED) == 2


def test_bearer_tokens_are_redacted() -> None:
    result = redact_text("Authorization: Bearer abcdef0123456789abcdef")
    assert result == f"Authorization: Bearer {REDACTED}"


def test_key_value_pairs_are_redacted() -> None:
    assert redact_text("api_key=hunter2hunter2") == f"api_key={REDACTED}"
    assert redact_text('"password": "correct horse battery"') == f'"password": "{REDACTED}"'
    assert redact_text("token: 'a b c'") == f"token: '{REDACTED}'"


def test_ordinary_text_is_untouched() -> None:
    text = "open VS Code on workspace 3 and Chromium on workspace 2"
    assert redact_text(text) == text


def test_nested_values_and_sensitive_keys() -> None:
    data = {
        "request": "hello",
        "openai_api_key": "whatever",
        "nested": [{"token": "x"}, "Bearer abcdefghijklmnopqrstuvwxyz"],
        "max_tokens": 512,
    }
    result = redact_value(data)
    assert result["request"] == "hello"
    assert result["openai_api_key"] == REDACTED
    assert result["nested"][0]["token"] == REDACTED
    assert REDACTED in result["nested"][1]
    assert result["max_tokens"] == 512
