from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "password",
    "passkey",
    "api_key",
    "apikey",
    "token",
    "authorization",
    "cookie",
    "secret",
    "csrf",
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passkey|api[_-]?key|token|authorization|cookie|secret|csrf)"
    r"\s*([=:])\s*([^\s,;]+)"
)
_URL = re.compile(r"https?://[^\s\"']+")


def redact_fields(value: object, *, key: str | None = None) -> object:
    """递归脱敏结构化日志字段；未知对象不调用 repr，避免隐式泄密。"""

    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_fields(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_fields(item) for item in value]
    return f"<{type(value).__name__}>"


def sanitize_message(message: str) -> str:
    """屏蔽常见 key=value 秘密，并移除日志 URL 的 query/fragment。"""

    redacted = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", message
    )
    return _URL.sub(lambda match: _sanitize_url(match.group(0)), redacted)


class JsonLogFormatter(logging.Formatter):
    """生产日志使用稳定 JSON；异常只记录类型，不序列化异常正文/traceback。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_message(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            payload["fields"] = redact_fields(fields)
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 外部 HTTP 客户端的请求日志可能包含 URL；应用只保留自己的安全摘要。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if not parsed.query and not parsed.fragment:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
