from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.domain.notification import (
    NotificationChannelKind,
    NotificationDeliveryState,
    NotificationSeverity,
)
from backend.app.domain.operation import OperationStatus
from backend.app.domain.site_config import SiteCredentialKind, SiteKind
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
_SITE_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in SiteKind)
_SITE_CREDENTIAL_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in SiteCredentialKind)
_NOTIFICATION_CHANNEL_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in NotificationChannelKind)
_NOTIFICATION_DELIVERY_STATE_SQL = ", ".join(
    f"'{state.value}'" for state in NotificationDeliveryState
)
_NOTIFICATION_SEVERITY_SQL = ", ".join(f"'{severity.value}'" for severity in NotificationSeverity)


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


class NotificationChannel(Base):
    __tablename__ = "notification_channel"
    __table_args__ = (
        CheckConstraint(f"type IN ({_NOTIFICATION_CHANNEL_KIND_SQL})", name="type"),
        CheckConstraint(
            "connection_status IN ('UNTESTED', 'OK', 'FAILED')", name="connection_status"
        ),
        CheckConstraint(
            "aggregation_window_seconds BETWEEN 1 AND 86400",
            name="aggregation_window_seconds",
        ),
        Index("ix_notification_channel_type_enabled", "type", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("secret.id", ondelete="SET NULL"), nullable=True
    )
    task_link_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    aggregation_window_seconds: Mapped[int] = mapped_column(nullable=False, default=300)
    connection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
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


class Site(Base):
    __tablename__ = "site"
    __table_args__ = (
        CheckConstraint(f"type IN ({_SITE_KIND_SQL})", name="type"),
        CheckConstraint(
            f"credential_kind IN ({_SITE_CREDENTIAL_KIND_SQL})", name="credential_kind"
        ),
        Index("ix_site_type_enabled", "type", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    credential_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("secret.id", ondelete="SET NULL"), nullable=True
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    connection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
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


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(f"state IN ({_NOTIFICATION_DELIVERY_STATE_SQL})", name="state"),
        CheckConstraint(f"severity IN ({_NOTIFICATION_SEVERITY_SQL})", name="severity"),
        CheckConstraint("pending_count >= 0", name="pending_count"),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        CheckConstraint("subject_kind IN ('TASK', 'SITE')", name="subject_kind"),
        CheckConstraint(
            "(subject_kind = 'TASK' AND task_id IS NOT NULL AND last_event_id IS NOT NULL "
            "AND subject_id = task_id) OR "
            "(subject_kind = 'SITE' AND task_id IS NULL AND last_event_id IS NULL)",
            name="subject_identity",
        ),
        UniqueConstraint(
            "channel_id",
            "subject_kind",
            "subject_id",
            "event_key",
            name="uq_notification_outbox_channel_subject_event_key",
        ),
        Index("ix_notification_outbox_due", "state", "next_attempt_at"),
        Index("ix_notification_outbox_task_updated", "task_id", "updated_at"),
        Index("ix_notification_outbox_subject_updated", "subject_kind", "subject_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    channel_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notification_channel.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=True
    )
    last_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_event.id", ondelete="CASCADE"), nullable=True
    )
    channel_version: Mapped[int] = mapped_column(nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    pending_count: Mapped[int] = mapped_column(nullable=False, default=1)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class PreflightSnapshotRecord(Base):
    __tablename__ = "preflight_snapshot"
    __table_args__ = (Index("ix_preflight_snapshot_task_created_at", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("unpack_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_version: Mapped[int] = mapped_column(nullable=False)
    normalized_unit_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_inventory_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskUnitRecord(Base):
    __tablename__ = "task_unit"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "source_inventory_digest",
            "normalized_unit_key",
            name="uq_task_unit_task_inventory_key",
        ),
        Index("ix_task_unit_task_inventory", "task_id", "source_inventory_digest"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    normalized_unit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_root: Mapped[str] = mapped_column(Text, nullable=False)
    source_inventory_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    length: Mapped[int] = mapped_column(nullable=False)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskCandidateRecord(Base):
    __tablename__ = "task_candidate"
    __table_args__ = (
        UniqueConstraint(
            "preflight_snapshot_id",
            "site_id",
            "torrent_id",
            name="uq_task_candidate_snapshot_site_torrent",
        ),
        Index("ix_task_candidate_task_created_at", "task_id", "created_at"),
        Index("ix_task_candidate_snapshot_score", "preflight_snapshot_id", "score"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    preflight_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("preflight_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    normalized_unit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    site_id: Mapped[str] = mapped_column(String(64), nullable=False)
    torrent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(nullable=False)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    selected_for_verification: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metainfo_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskReviewRevisionRecord(Base):
    __tablename__ = "task_review_revision"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint(
            "task_unit_id",
            "preflight_snapshot_id",
            "version",
            name="uq_task_review_unit_snapshot_version",
        ),
        Index("ix_task_review_task_created_at", "task_id", "created_at"),
        Index(
            "ix_task_review_unit_snapshot_version",
            "task_unit_id",
            "preflight_snapshot_id",
            "version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    task_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_unit.id", ondelete="CASCADE"), nullable=False
    )
    preflight_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("preflight_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    approved_candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_candidate.id", ondelete="SET NULL"), nullable=True
    )
    rejected_candidate_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    manual_mappings: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_reverification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskReviewVerificationRecord(Base):
    __tablename__ = "task_review_verification"
    __table_args__ = (
        UniqueConstraint("review_revision_id", name="uq_task_review_verification_revision"),
        Index("ix_task_review_verification_task_created_at", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    review_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_review_revision.id", ondelete="CASCADE"), nullable=False
    )
    review_version: Mapped[int] = mapped_column(nullable=False)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    task_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_unit.id", ondelete="CASCADE"), nullable=False
    )
    preflight_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("preflight_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_candidate.id", ondelete="CASCADE"), nullable=False
    )
    source_inventory_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metainfo_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_level: Mapped[str] = mapped_column(String(32), nullable=False)
    mappings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    verification_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskExecutionGateRecord(Base):
    __tablename__ = "task_execution_gate"
    __table_args__ = (
        UniqueConstraint("gate_digest", name="uq_task_execution_gate_gate_digest"),
        Index("ix_task_execution_gate_unit_created_at", "task_unit_id", "created_at"),
        Index("ix_task_execution_gate_task_eligible", "task_id", "eligible"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    task_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_unit.id", ondelete="CASCADE"), nullable=False
    )
    preflight_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("preflight_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    review_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_review_revision.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_candidate.id", ondelete="SET NULL"), nullable=True
    )
    review_verification_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_review_verification.id", ondelete="SET NULL"), nullable=True
    )
    task_version: Mapped[int] = mapped_column(nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    client_check_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metainfo_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    gate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskExecutionPlanRecord(Base):
    __tablename__ = "task_execution_plan"
    __table_args__ = (
        UniqueConstraint("plan_digest", name="uq_task_execution_plan_plan_digest"),
        Index("ix_task_execution_plan_unit_created_at", "task_unit_id", "created_at"),
        Index("ix_task_execution_plan_task_ready", "task_id", "ready"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    task_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_unit.id", ondelete="CASCADE"), nullable=False
    )
    execution_gate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_execution_gate.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_candidate.id", ondelete="CASCADE"), nullable=False
    )
    task_version: Mapped[int] = mapped_column(nullable=False)
    target_root: Mapped[str] = mapped_column(Text, nullable=False)
    target_device: Mapped[int] = mapped_column(nullable=False)
    verification_level: Mapped[str] = mapped_column(String(32), nullable=False)
    client_check_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blocked_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    estimated_download_bytes_upper_bound: Mapped[int] = mapped_column(nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
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


class TaskActionReceipt(Base):
    __tablename__ = "task_action_receipt"
    __table_args__ = (
        CheckConstraint("state IN ('PENDING', 'SUCCEEDED', 'FAILED')", name="state"),
        UniqueConstraint(
            "actor_kind",
            "actor_id",
            "idempotency_key_digest",
            name="uq_task_action_receipt_actor_key",
        ),
        Index("ix_task_action_receipt_task_created_at", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="CASCADE"), nullable=False
    )
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
