from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class TaskDefinitionKind(StrEnum):
    MANUAL = "MANUAL"
    MONITOR = "MONITOR"


class TaskDefinitionStatus(StrEnum):
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    SITE_UNAVAILABLE = "SITE_UNAVAILABLE"
    ERROR = "ERROR"


class TaskSourceKind(StrEnum):
    DOWNLOADER = "DOWNLOADER"
    DIRECTORY = "DIRECTORY"


class TaskStorageMode(StrEnum):
    HARDLINK = "HARDLINK"
    SYMLINK = "SYMLINK"
    COPY = "COPY"


class TaskConflictPolicy(StrEnum):
    VERIFY_REUSE_OR_STOP = "VERIFY_REUSE_OR_STOP"
    SKIP = "SKIP"
    RENAME = "RENAME"
    OVERWRITE = "OVERWRITE"


class TaskInitialScope(StrEnum):
    NEW_ONLY = "NEW_ONLY"
    INCLUDE_EXISTING = "INCLUDE_EXISTING"


class TaskOverlapPolicy(StrEnum):
    SKIP = "SKIP"
    RUN_ONCE_AFTER = "RUN_ONCE_AFTER"


class TaskExecutionTrigger(StrEnum):
    MANUAL = "MANUAL"
    CRON = "CRON"
    IMMEDIATE_SCAN = "IMMEDIATE_SCAN"
    FAILED_RETRY = "FAILED_RETRY"
    SYSTEM_RECOVERY = "SYSTEM_RECOVERY"


class TaskExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskExecutionPhase(StrEnum):
    WAITING = "WAITING"
    DISCOVERING = "DISCOVERING"
    ANALYZING = "ANALYZING"
    SCANNING_SITE = "SCANNING_SITE"
    PREPARING = "PREPARING"
    UNPACKING = "UNPACKING"
    OUTPUTTING = "OUTPUTTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


DEFAULT_VIDEO_EXTENSIONS = (".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv")
DEFAULT_ARCHIVE_EXTENSIONS = (".rar", ".zip", ".7z", ".tar", ".gz")
DEFAULT_EXCLUDE_NAMES = ("sample", "trailer")
DEFAULT_TEMP_PATTERNS = ("*.part", "*.tmp", "*.crdownload", "*.!qB", "*.aria2")
DEFAULT_RETRY_INTERVALS_SECONDS = (60, 300, 900)


_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def normalize_cron_expression(value: str) -> str:
    """Validate the v0.1.5 five-field cron subset and return normalized whitespace."""

    fields = value.strip().split()
    if len(fields) != 5:
        raise ValueError("Cron 表达式必须包含 5 个字段：分 时 日 月 周")
    for field, bounds in zip(fields, _CRON_BOUNDS, strict=True):
        _validate_cron_field(field, *bounds)
    return " ".join(fields)


def cron_matches(value: str, instant: datetime, *, timezone: str) -> bool:
    """Return whether an aware instant matches the supported five-field cron expression."""

    expression = normalize_cron_expression(value)
    if instant.tzinfo is None:
        raise ValueError("Cron 匹配时间必须包含时区")
    local = instant.astimezone(ZoneInfo(timezone))
    minute, hour, day, month, weekday = expression.split()
    cron_weekday = (local.weekday() + 1) % 7
    day_matches = _cron_field_matches(day, local.day, 1, 31)
    weekday_matches = _cron_field_matches(weekday, cron_weekday, 0, 7, sunday_alias=True)
    day_wildcard = day == "*"
    weekday_wildcard = weekday == "*"
    if day_wildcard and weekday_wildcard:
        calendar_day_matches = True
    elif day_wildcard:
        calendar_day_matches = weekday_matches
    elif weekday_wildcard:
        calendar_day_matches = day_matches
    else:
        calendar_day_matches = day_matches or weekday_matches
    return (
        _cron_field_matches(minute, local.minute, 0, 59)
        and _cron_field_matches(hour, local.hour, 0, 23)
        and calendar_day_matches
        and _cron_field_matches(month, local.month, 1, 12)
    )


def next_cron_run(value: str, after: datetime, *, timezone: str) -> datetime:
    """Find the next matching minute after ``after`` and return it in UTC."""

    expression = normalize_cron_expression(value)
    if after.tzinfo is None:
        raise ValueError("Cron 基准时间必须包含时区")
    local_zone = ZoneInfo(timezone)
    candidate = after.astimezone(local_zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366 * 2):
        if cron_matches(expression, candidate, timezone=timezone):
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError("Cron 表达式在未来两年内没有可执行时间")


def _validate_cron_field(field: str, minimum: int, maximum: int) -> None:
    if not field:
        raise ValueError("Cron 字段不能为空")
    for part in field.split(","):
        if not part:
            raise ValueError("Cron 列表项不能为空")
        base, separator, step_text = part.partition("/")
        if separator and (not step_text.isdigit() or int(step_text) <= 0):
            raise ValueError("Cron 步长必须是正整数")
        if base == "*":
            continue
        if "-" in base:
            start_text, dash, end_text = base.partition("-")
            if dash != "-" or not start_text.isdigit() or not end_text.isdigit():
                raise ValueError("Cron 范围格式无效")
            start = int(start_text)
            end = int(end_text)
            if start > end or start < minimum or end > maximum:
                raise ValueError("Cron 范围超出允许值")
            continue
        if not base.isdigit():
            raise ValueError("Cron 字段仅支持数字、*、范围、列表和步长")
        number = int(base)
        if number < minimum or number > maximum:
            raise ValueError("Cron 数值超出允许范围")


def _cron_field_matches(
    field: str,
    value: int,
    minimum: int,
    maximum: int,
    *,
    sunday_alias: bool = False,
) -> bool:
    target = 0 if sunday_alias and value == 7 else value
    for part in field.split(","):
        base, separator, step_text = part.partition("/")
        step = int(step_text) if separator else 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            start, end = int(start_text), int(end_text)
        else:
            start = int(base)
            end = maximum if separator else start
        for candidate in range(start, end + 1, step):
            normalized = 0 if sunday_alias and candidate == 7 else candidate
            if normalized == target:
                return True
    return False
