import logging
import sys

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
        f"password={canary} url=https://example.invalid/path?token={canary}#fragment"
    )

    assert canary not in message
    assert "password=[REDACTED]" in message
    assert "https://example.invalid/path" in message
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
