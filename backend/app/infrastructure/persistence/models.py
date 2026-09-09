from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.types import UTCDateTime


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid4())


_TASK_STATUS_SQL = ", ".join(f"'{status.value}'" for status in TaskStatus)
_OPERATION_STATUS_SQL = ", ".join(f"'{status.value}'" for status in OperationStatus)
_DOWNLOADER_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in DownloaderKind)


class Administrator(Base):
    __tablename__ = "administrator"
    __table_args__ = (CheckConstraint("id = 'admin'", name="singleton"),)

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="admin")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AdminSession(Base):
    __tablename__ = "admin_session"
    __table_args__ = (Index("ix_admin_session_expires_at", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    administrator_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("administrator.id", ondelete="CASCADE"),
        nullable=False,
        default="admin",
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ApiToken(Base):
    __tablename__ = "api_token"
    __table_args__ = (
        Index("ix_api_token_expires_at", "expires_at"),
        Index("ix_api_token_revoked_at", "revoked_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class SecretRecord(Base):
    __tablename__ = "secret"
    __table_args__ = (Index("ix_secret_kind", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Downloader(Base):
    __tablename__ = "downloader"
    __table_args__ = (
        CheckConstraint(f"type IN ({_DOWNLOADER_KIND_SQL})", name="type"),
        Index("ix_downloader_type_enabled", "type", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("secret.id", ondelete="SET NULL"), nullable=True
    )
    monitor_rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    path_mappings: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    connection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    path_mapping_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_path_diagnostic_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class UnpackTask(Base):
    __tablename__ = "unpack_task"
    __table_args__ = (
        CheckConstraint(f"status IN ({_TASK_STATUS_SQL})", name="status"),
        Index("ix_unpack_task_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_downloader_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_unit_key: Mapped[str] = mapped_column(String(512), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskEvent(Base):
    __tablename__ = "task_event"
    __table_args__ = (Index("ix_task_event_task_created_at", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("unpack_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class OperationJournal(Base):
    __tablename__ = "operation_journal"
    __table_args__ = (
        CheckConstraint(f"status IN ({_OPERATION_STATUS_SQL})", name="status"),
        Index("ix_operation_journal_task_status", "task_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("unpack_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    intent: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
