from __future__ import annotations

import logging

from backend.app.infrastructure.app_logging import AlembicContextNoiseFilter
from backend.app.main import _should_log_http_request


def test_successful_polling_is_quiet_but_failures_and_slow_requests_are_not() -> None:
    for path in (
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/notifications/inbox/unread-count",
        "/api/v1/auth/me",
    ):
        assert not _should_log_http_request("GET", path, 200, 8.0)
        assert _should_log_http_request("GET", path, 500, 8.0)
        assert _should_log_http_request("GET", path, 302, 8.0)
        assert _should_log_http_request("GET", path, 200, 1100.0)
        assert _should_log_http_request("POST", path, 200, 8.0)
    assert _should_log_http_request("GET", "/api/v1/system/logs", 200, 8.0)
    assert _should_log_http_request("POST", "/api/v1/cookiecloud/sync", 200, 8.0)


def test_alembic_only_drops_the_two_repeated_info_boilerplates() -> None:
    noise_filter = AlembicContextNoiseFilter()

    def log_record(message: str, level: int) -> logging.LogRecord:
        return logging.LogRecord(
            "alembic.runtime.migration", level, "script.py", 1, message, (), None
        )

    assert not noise_filter.filter(log_record("Context impl SQLiteImpl.", logging.INFO))
    assert not noise_filter.filter(log_record("Will assume non-transactional DDL.", logging.INFO))
    assert noise_filter.filter(log_record("Running upgrade 0045 -> 0046", logging.INFO))
    assert noise_filter.filter(log_record("Migration failed", logging.ERROR))
