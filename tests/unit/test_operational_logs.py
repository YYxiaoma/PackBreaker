from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from backend.app.infrastructure.app_logging import configure_logging
from backend.app.infrastructure.operational_logs import query_operational_logs


def _flush_root_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_rotating_operational_log_is_bounded_persistent_and_redacted(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    canary = "PACKBREAKER-OPLOG-CANARY-4d12"
    max_bytes = 64 * 1024
    backup_count = 2
    try:
        configure_logging(
            "DEBUG",
            log_dir=log_dir,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        logger = logging.getLogger("packbreaker.test.operational")
        for index in range(180):
            logger.info("bounded filler %s %s", index, "x" * 600)
        logger.error(
            f"token={canary} https://user:{canary}@example.invalid/private/{canary}?api_key={canary}",
            extra={
                "fields": {
                    "password": canary,
                    "trace_id": "safe-trace",
                    "callback_url": (
                        f"https://user:{canary}@fields.invalid/private/{canary}?token={canary}"
                    ),
                }
            },
        )
        _flush_root_handlers()

        files = sorted(log_dir.glob("packbreaker.jsonl*"))
        assert 1 <= len(files) <= backup_count + 1
        assert all((path.stat().st_mode & 0o077) == 0 for path in files)
        assert (log_dir.stat().st_mode & 0o077) == 0
        assert sum(path.stat().st_size for path in files) <= (backup_count + 1) * (
            max_bytes + 256 * 1024
        )

        result = query_operational_logs(
            log_dir,
            backup_count=backup_count,
            max_file_bytes=max_bytes,
            window_minutes=60,
            limit=500,
            query="safe-trace",
        )
        assert len(result.entries) == 1
        entry = result.entries[0]
        encoded = str(entry.as_dict())
        assert canary not in encoded
        assert "user:" not in entry.message
        assert "/private/" not in entry.message
        assert "https://example.invalid" in entry.message
        assert entry.fields["password"] == "[REDACTED]"
        assert entry.fields["trace_id"] == "safe-trace"
        assert entry.fields["callback_url"] == "https://fields.invalid"
    finally:
        configure_logging("WARNING")


def test_operational_log_writer_rejects_symlink_target(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside.log"
    outside.write_text("must remain untouched\n", encoding="utf-8")
    os.symlink(outside, log_dir / "packbreaker.jsonl")

    try:
        with pytest.raises(RuntimeError, match="符号链接"):
            configure_logging("INFO", log_dir=log_dir, max_bytes=64 * 1024, backup_count=1)
        assert outside.read_text(encoding="utf-8") == "must remain untouched\n"
    finally:
        configure_logging("WARNING")


def test_operational_log_query_filters_limits_and_ignores_symlink(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    try:
        configure_logging("INFO", log_dir=log_dir, max_bytes=64 * 1024, backup_count=1)
        logger = logging.getLogger("packbreaker.query")
        logger.info("first searchable event", extra={"fields": {"trace_id": "trace-one"}})
        logger.warning("second searchable event", extra={"fields": {"trace_id": "trace-two"}})
        _flush_root_handlers()

        limited = query_operational_logs(
            log_dir,
            backup_count=1,
            max_file_bytes=64 * 1024,
            window_minutes=60,
            limit=1,
            query="searchable",
        )
        assert len(limited.entries) == 1
        assert limited.truncated is True
        assert limited.entries[0].level == "WARNING"

        warning_only = query_operational_logs(
            log_dir,
            backup_count=1,
            max_file_bytes=64 * 1024,
            level="WARNING",
        )
        assert [entry.fields["trace_id"] for entry in warning_only.entries] == ["trace-two"]

        real_backup = tmp_path / "outside.jsonl"
        real_backup.write_text('{"message":"must not follow"}\n', encoding="utf-8")
        os.symlink(real_backup, log_dir / "packbreaker.jsonl.1")
        still_safe = query_operational_logs(
            log_dir,
            backup_count=1,
            max_file_bytes=64 * 1024,
        )
        assert all(entry.message != "must not follow" for entry in still_safe.entries)

        with pytest.raises(ValueError, match="查询窗口"):
            query_operational_logs(
                log_dir,
                backup_count=1,
                max_file_bytes=64 * 1024,
                window_minutes=0,
            )
    finally:
        configure_logging("WARNING")
