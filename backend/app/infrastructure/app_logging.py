from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
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
_MAX_MESSAGE_CHARS = 4096
_MAX_FIELDS_BYTES = 16 * 1024


def redact_fields(value: object, *, key: str | None = None) -> object:
    """递归脱敏结构化日志字段；未知对象不调用 repr，避免隐式泄密。"""

    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, str):
        return sanitize_message(value)
    if value is None or isinstance(value, (bool, int, float)):
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
            "logger": record.name[:256],
            "message": sanitize_message(record.getMessage())[:_MAX_MESSAGE_CHARS],
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            redacted_fields = redact_fields(fields)
            encoded_fields = json.dumps(redacted_fields, ensure_ascii=False, separators=(",", ":"))
            payload["fields"] = (
                {"truncated": True}
                if len(encoded_fields.encode("utf-8")) > _MAX_FIELDS_BYTES
                else redacted_fields
            )
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SecureRotatingFileHandler(RotatingFileHandler):
    """使用 no-follow 打开 0600 日志文件；旧文件由 rename 保留相同权限。"""

    def _open(self) -> Any:
        path = Path(self.baseFilename)
        if path.is_symlink():
            raise RuntimeError("日志文件不能是符号链接")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(
                fd,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
            )
        except BaseException:
            os.close(fd)
            raise


def configure_logging(
    level: str,
    *,
    log_dir: Path | None = None,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 4,
) -> None:
    if max_bytes < 64 * 1024 or backup_count < 1:
        raise ValueError("日志轮转配置无效")
    formatter = JsonLogFormatter()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in tuple(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(console_handler)
    if log_dir is not None:
        directory = _ensure_log_directory(log_dir)
        file_handler = SecureRotatingFileHandler(
            directory / "packbreaker.jsonl",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
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
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if hostname is None:
        return "<invalid-url>"
    safe_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    netloc = safe_host if port is None else f"{safe_host}:{port}"
    # URL path 也可能承载 bot token / API key；日志只保留 origin。
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _ensure_log_directory(path: Path) -> Path:
    if path.is_symlink():
        raise RuntimeError("日志目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("日志路径不是目录")
    os.chmod(path, 0o700)
    return path
