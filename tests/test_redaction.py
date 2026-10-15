from __future__ import annotations

import pytest

from app.utils.redaction import REDACTED, is_sensitive_key, redact, redact_value


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefghijklmnop1234",
        "api_key=abc123secretvalue",
        'config {"password": "hunter2"}',
        "my password is hunter2",
        "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123",
        "url https://bob:s3cret@example.com/path",
        "card 4111 1111 1111 1111 on file",
    ],
)
def test_secrets_are_redacted(text: str) -> None:
    result = redact(text)
    assert REDACTED in result
    for secret in ("hunter2", "abc123secretvalue", "abcdefghijklmnop1234", "s3cret", "4111 1111 1111 1111"):
        assert secret not in result


def test_ordinary_text_untouched() -> None:
    text = "Opened Chrome in 1234 ms; CPU at 37%; order 1234567890123 shipped"
    assert redact(text) == text


@pytest.mark.parametrize("key", ["password", "api_key", "apiKey", "OPENAI_API_KEY", "access_token", "client_secret", "Cookie"])
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["token_count", "subject_key", "hotkey", "tool", "path", "password_length"])
def test_non_sensitive_keys(key: str) -> None:
    assert not is_sensitive_key(key)


def test_nested_values() -> None:
    data = {"user": "labib", "auth": {"api_key": "zzz", "items": ["password=abc"]}, "token_count": 42}
    result = redact_value(data)
    assert result["user"] == "labib"
    assert result["auth"]["api_key"] == REDACTED
    assert result["auth"]["items"] == ["password=" + REDACTED]
    assert result["token_count"] == 42


def test_prefixed_names_like_api_token_are_redacted_too() -> None:
    from app.utils.redaction import redact

    for line in ('API_TOKEN = "sk_live_abcdefghijklmnop1234567890"', "HOME_ASSISTANT_TOKEN=eyabc123def456",
                 "DB_PASSWORD: hunter2hunter2", "stripe_secret='abcd1234efgh'"):
        out = redact(line)
        assert "[REDACTED]" in out and not any(v in out for v in ("sk_live", "eyabc123", "hunter2", "abcd1234efgh")), out
    assert redact("token_count = 5") == "token_count = 5"  # a count isn't a secret
    assert redact("mytoken = 5") == "mytoken = 5"
