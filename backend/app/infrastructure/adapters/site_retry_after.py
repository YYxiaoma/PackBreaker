"""Shared bounded parsing of HTTP Retry-After for reviewed site adapters."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx2


def retry_after_seconds(response: httpx2.Response) -> float | None:
    """Support delay-seconds and HTTP-date; fail closed on explicit bad values."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None or retry_at.utcoffset() is None:
                return float("inf")
            delay = max(0.0, (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return float("inf")
    if not isfinite(delay) or delay < 0 or delay > 86_400:
        return float("inf")
    return delay
