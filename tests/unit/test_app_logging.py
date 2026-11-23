import logging
import sys

import pytest

from backend.app.infrastructure.app_logging import JsonLogFormatter, redact_fields, sanitize_message


def test_structured_log_fields_redact_sensitive_values() -> None:
    canary = "PACKBREAKER-LOG-CANARY-3ca4"
    redacted = redact_fields(
        {
            "name": "downloader",
            "password": canary,
            "nested": {"api_key": canary, "safe": "ok"},
            "tokens": [canary],
        }
    )

    assert redacted == {
        "name": "downloader",
        "password": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "safe": "ok"},
        "tokens": "[REDACTED]",
    }
    assert canary not in str(redacted)


def test_log_message_redacts_assignments_and_url_query() -> None:
    canary = "PACKBREAKER-LOG-CANARY-7c11"
    message = sanitize_message(
        f"password={canary} url=https://user:{canary}@example.invalid/private/{canary}?token={canary}#fragment"
    )

    assert canary not in message
    assert "password=[REDACTED]" in message
    assert "https://example.invalid" in message
    assert "user:" not in message
    assert "/private/" not in message
    assert "?" not in message


def test_json_formatter_never_serializes_exception_message_or_sensitive_fields() -> None:
    canary = "PACKBREAKER-LOG-CANARY-bd20"
    formatter = JsonLogFormatter()
    try:
        raise RuntimeError(canary)
    except RuntimeError:
        record = logging.LogRecord(
            name="packbreaker.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request.failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    record.fields = {"authorization": canary, "status_code": 500}

    rendered = formatter.format(record)

    assert canary not in rendered
    assert '"exception":"RuntimeError"' in rendered
    assert '"authorization":"[REDACTED]"' in rendered


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "bot_token=123456:ABCDEFGHIJKLMNOP",
        "client_secret=synthetic-client-secret",
        "send_key=SCT123456789",
        "refresh_token=rt-synthetic-value",
        "webhook_secret=whsec-synthetic",
    ],
)
def test_log_message_redacts_underscore_and_bearer_credentials(message: str) -> None:
    """下划线前缀键名与 Bearer 后置凭证都必须整体脱敏。"""
    rendered = sanitize_message(message)
    assert "[REDACTED]" in rendered
    for fragment in (
        "eyJhbGciOiJIUzI1NiJ9",
        "ABCDEFGHIJKLMNOP",
        "synthetic-client-secret",
        "SCT123456789",
        "rt-synthetic-value",
        "whsec-synthetic",
    ):
        assert fragment not in rendered


def test_redact_fields_keeps_key_context_for_key_value_pairs() -> None:
    canary = "PACKBREAKER-LOG-CANARY-pair9"
    assert redact_fields(("token", canary)) == ["token", "[REDACTED]"]
    assert redact_fields({"pairs": [("api_key", canary)]}) == {"pairs": [["api_key", "[REDACTED]"]]}
    assert canary not in str(redact_fields({"pairs": [("api_key", canary)]}))
