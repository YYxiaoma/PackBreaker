from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from backend.app.infrastructure.app_logging import redact_fields, sanitize_message

DEFAULT_LOG_WINDOW_MINUTES = 60
MAX_LOG_WINDOW_MINUTES = 7 * 24 * 60
DEFAULT_LOG_QUERY_LIMIT = 200
MAX_LOG_QUERY_LIMIT = 500
MAX_LOG_EXPORT_LIMIT = 2000
LOG_FILENAME = "packbreaker.jsonl"

OperationalLogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_ALLOWED_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True, slots=True)
class OperationalLogEntry:
    timestamp: datetime
    level: OperationalLogLevel
    logger: str
    message: str
    fields: dict[str, Any]
    exception: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "fields": self.fields,
            "exception": self.exception,
        }


@dataclass(frozen=True, slots=True)
class OperationalLogQueryResult:
    window_minutes: int
    limit: int
    truncated: bool
    entries: tuple[OperationalLogEntry, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "window_minutes": self.window_minutes,
            "limit": self.limit,
            "count": len(self.entries),
            "truncated": self.truncated,
            "items": [entry.as_dict() for entry in self.entries],
        }


def operational_log_path(log_dir: Path) -> Path:
    return log_dir / LOG_FILENAME


def query_operational_logs(
    log_dir: Path,
    *,
    backup_count: int,
    max_file_bytes: int,
    window_minutes: int = DEFAULT_LOG_WINDOW_MINUTES,
    limit: int = DEFAULT_LOG_QUERY_LIMIT,
    level: OperationalLogLevel | None = None,
    query: str | None = None,
    now: datetime | None = None,
) -> OperationalLogQueryResult:
    """读取受控轮转日志；只返回再次脱敏后的有限窗口结果。"""

    if not 1 <= window_minutes <= MAX_LOG_WINDOW_MINUTES:
        raise ValueError("日志查询窗口超出允许范围")
    if not 1 <= limit <= MAX_LOG_EXPORT_LIMIT:
        raise ValueError("日志查询条数超出允许范围")
    if backup_count < 1 or max_file_bytes < 1:
        raise ValueError("日志轮转配置无效")
    if level is not None and level not in _ALLOWED_LEVELS:
        raise ValueError("日志级别无效")
    normalized_query = query.strip().casefold() if query is not None else ""
    if len(normalized_query) > 128:
        raise ValueError("日志搜索词最长 128 个字符")

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = timestamp - timedelta(minutes=window_minutes)
    entries: list[OperationalLogEntry] = []
    base = operational_log_path(log_dir)
    candidates = [base, *(Path(f"{base}.{index}") for index in range(1, backup_count + 1))]
    for path in candidates:
        entries.extend(_read_log_file(path, max_file_bytes=max_file_bytes))

    matching = [
        entry
        for entry in entries
        if entry.timestamp >= cutoff
        and entry.timestamp <= timestamp + timedelta(minutes=1)
        and (level is None or entry.level == level)
        and (
            not normalized_query
            or normalized_query
            in " ".join(
                (
                    entry.logger,
                    entry.message,
                    json.dumps(entry.fields, ensure_ascii=False, sort_keys=True),
                    entry.exception or "",
                )
            ).casefold()
        )
    ]
    matching.sort(key=lambda entry: entry.timestamp, reverse=True)
    return OperationalLogQueryResult(
        window_minutes=window_minutes,
        limit=limit,
        truncated=len(matching) > limit,
        entries=tuple(matching[:limit]),
    )


def _read_log_file(path: Path, *, max_file_bytes: int) -> list[OperationalLogEntry]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return []
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return []
        # RotatingFileHandler may exceed maxBytes by one final record before rollover.
        if metadata.st_size > max_file_bytes + 256 * 1024:
            return []
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace", closefd=True) as handle:
            fd = -1
            return [entry for line in handle if (entry := _parse_log_line(line)) is not None]
    finally:
        if fd >= 0:
            os.close(fd)


def _parse_log_line(line: str) -> OperationalLogEntry | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_timestamp = payload.get("timestamp")
    raw_level = payload.get("level")
    raw_logger = payload.get("logger")
    raw_message = payload.get("message")
    if not isinstance(raw_timestamp, str):
        return None
    if not isinstance(raw_level, str) or raw_level not in _ALLOWED_LEVELS:
        return None
    if not isinstance(raw_logger, str) or not isinstance(raw_message, str):
        return None
    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    raw_fields = payload.get("fields", {})
    redacted_fields = redact_fields(raw_fields)
    fields: dict[str, Any]
    if isinstance(redacted_fields, dict):
        fields = cast(dict[str, Any], redacted_fields)
    else:
        fields = {"value": redacted_fields}
    exception = payload.get("exception")
    if exception is not None and not isinstance(exception, str):
        exception = None
    return OperationalLogEntry(
        timestamp=timestamp,
        level=cast(OperationalLogLevel, raw_level),
        logger=raw_logger[:256],
        message=sanitize_message(raw_message)[:4096],
        fields=fields,
        exception=None if exception is None else exception[:128],
    )
