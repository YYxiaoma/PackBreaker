from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.domain.ai_agent import AIConnectionStatus, AIProviderKind
from backend.app.domain.notification import (
    NotificationChannelKind,
    NotificationDeliveryState,
    NotificationSeverity,
)
from backend.app.domain.operation import OperationStatus
from backend.app.domain.site_config import (
    PERSISTED_SITE_CREDENTIAL_KINDS,
    PERSISTED_SITE_KINDS,
)
from backend.app.domain.task_definition import (
    TaskConflictPolicy,
    TaskDefinitionKind,
    TaskDefinitionStatus,
    TaskExecutionPhase,
    TaskExecutionStatus,
    TaskExecutionTrigger,
    TaskInitialScope,
    TaskOverlapPolicy,
    TaskSourceKind,
    TaskStorageMode,
)
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
_SITE_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in sorted(PERSISTED_SITE_KINDS))
_SITE_CREDENTIAL_KIND_SQL = ", ".join(
    f"'{kind.value}'" for kind in sorted(PERSISTED_SITE_CREDENTIAL_KINDS)
)
_NOTIFICATION_CHANNEL_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in NotificationChannelKind)
_NOTIFICATION_DELIVERY_STATE_SQL = ", ".join(
    f"'{state.value}'" for state in NotificationDeliveryState
)
_NOTIFICATION_SEVERITY_SQL = ", ".join(f"'{severity.value}'" for severity in NotificationSeverity)
_AI_PROVIDER_KIND_SQL = ", ".join(f"'{kind.value}'" for kind in AIProviderKind)
_AI_CONNECTION_STATUS_SQL = ", ".join(f"'{status.value}'" for status in AIConnectionStatus)
_TASK_DEFINITION_KIND_SQL = ", ".join(f"'{value.value}'" for value in TaskDefinitionKind)
_TASK_DEFINITION_STATUS_SQL = ", ".join(f"'{value.value}'" for value in TaskDefinitionStatus)
_TASK_SOURCE_KIND_SQL = ", ".join(f"'{value.value}'" for value in TaskSourceKind)
_TASK_STORAGE_MODE_SQL = ", ".join(f"'{value.value}'" for value in TaskStorageMode)
_TASK_CONFLICT_POLICY_SQL = ", ".join(f"'{value.value}'" for value in TaskConflictPolicy)
_TASK_INITIAL_SCOPE_SQL = ", ".join(f"'{value.value}'" for value in TaskInitialScope)
_TASK_OVERLAP_POLICY_SQL = ", ".join(f"'{value.value}'" for value in TaskOverlapPolicy)
_TASK_EXECUTION_TRIGGER_SQL = ", ".join(f"'{value.value}'" for value in TaskExecutionTrigger)
_TASK_EXECUTION_STATUS_SQL = ", ".join(f"'{value.value}'" for value in TaskExecutionStatus)
_TASK_EXECUTION_PHASE_SQL = ", ".join(f"'{value.value}'" for value in TaskExecutionPhase)


class BackupPolicy(Base):
    __tablename__ = "backup_policy"
    __table_args__ = (
        CheckConstraint("id = 'default'", name="singleton"),
        CheckConstraint("interval_hours BETWEEN 1 AND 168", name="interval_hours"),
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention_days"),
        CheckConstraint("keep_latest BETWEEN 1 AND 100", name="keep_latest"),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interval_hours: Mapped[int] = mapped_column(nullable=False, default=24)
    retention_days: Mapped[int] = mapped_column(nullable=False, default=30)
    keep_latest: Mapped[int] = mapped_column(nullable=False, default=3)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Administrator(Base):
    __tablename__ = "administrator"
    __table_args__ = (
        CheckConstraint("id = 'admin'", name="singleton"),
        Index("ux_administrator_username", "username", unique=True),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="admin")
    username: Mapped[str] = mapped_column(String(80), nullable=False, default="admin")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
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
    event_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    proxy_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proxy_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_port: Mapped[int | None] = mapped_column(nullable=True)
    proxy_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    connection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AdminNotification(Base):
    __tablename__ = "admin_notification"
    __table_args__ = (
        CheckConstraint(f"severity IN ({_NOTIFICATION_SEVERITY_SQL})", name="severity"),
        Index("ix_admin_notification_read_created", "read_at", "created_at"),
        UniqueConstraint("dedup_key", name="uq_admin_notification_dedup_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AIAgentSetting(Base):
    __tablename__ = "ai_agent_setting"
    __table_args__ = (
        CheckConstraint("id = 'default'", name="singleton"),
        CheckConstraint(f"provider_kind IN ({_AI_PROVIDER_KIND_SQL})", name="provider_kind"),
        CheckConstraint(
            f"connection_status IN ({_AI_CONNECTION_STATUS_SQL})", name="connection_status"
        ),
        CheckConstraint("request_timeout_seconds BETWEEN 1 AND 120", name="request_timeout"),
        CheckConstraint("max_context_messages BETWEEN 2 AND 100", name="max_context_messages"),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="OPENAI")
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="https://api.openai.com/v1")
    api_key_secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("secret.id", ondelete="SET NULL"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    request_timeout_seconds: Mapped[int] = mapped_column(nullable=False, default=30)
    max_context_messages: Mapped[int] = mapped_column(nullable=False, default=20)
    data_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    connection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AIChannelBinding(Base):
    __tablename__ = "ai_channel_binding"
    __table_args__ = (
        CheckConstraint("kind = 'TELEGRAM'", name="kind"),
        CheckConstraint("idle_timeout_minutes BETWEEN 5 AND 10080", name="idle_timeout"),
        CheckConstraint("max_context_messages BETWEEN 2 AND 100", name="max_context_messages"),
        CheckConstraint("last_update_id >= 0", name="last_update_id"),
        CheckConstraint("version >= 1", name="version"),
        UniqueConstraint(
            "notification_channel_id", name="uq_ai_channel_binding_notification_channel_id"
        ),
        Index("ix_ai_channel_binding_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="TELEGRAM")
    notification_channel_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("notification_channel.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_chat_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_user_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    idle_timeout_minutes: Mapped[int] = mapped_column(nullable=False, default=60)
    max_context_messages: Mapped[int] = mapped_column(nullable=False, default=20)
    last_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AIConversation(Base):
    __tablename__ = "ai_conversation"
    __table_args__ = (
        Index("ix_ai_conversation_binding_last_message", "binding_id", "last_message_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    binding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_channel_binding.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    last_message_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    context_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AIMessage(Base):
    __tablename__ = "ai_message"
    __table_args__ = (
        CheckConstraint("role IN ('USER', 'ASSISTANT')", name="role"),
        Index("ix_ai_message_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_conversation.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_summary: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


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
    request_timeout_seconds: Mapped[int] = mapped_column(nullable=False, default=15)
    search_interval_seconds: Mapped[int] = mapped_column(nullable=False, default=0)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    browser_emulation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proxy_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proxy_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_port: Mapped[int | None] = mapped_column(nullable=True)
    proxy_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    connection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskDefinition(Base):
    __tablename__ = "task_definition"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_TASK_DEFINITION_KIND_SQL})", name="kind"),
        CheckConstraint(f"status IN ({_TASK_DEFINITION_STATUS_SQL})", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_task_definition_kind_status", "kind", "status"),
        Index("ix_task_definition_site_id", "site_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    site_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("site.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskSchedule(Base):
    __tablename__ = "task_schedule"
    __table_args__ = (
        UniqueConstraint("task_definition_id", name="uq_task_schedule_definition"),
        Index("ix_task_schedule_next_run_at", "next_run_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_definition.id", ondelete="CASCADE"), nullable=False
    )
    cron_expression: Mapped[str] = mapped_column(String(160), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_successful_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    scan_checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskSource(Base):
    __tablename__ = "task_source"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_TASK_SOURCE_KIND_SQL})", name="kind"),
        CheckConstraint(
            "(kind = 'DOWNLOADER' AND directory_path IS NULL) OR "
            "(kind = 'DIRECTORY' AND downloader_id IS NULL AND directory_path IS NOT NULL)",
            name="binding",
        ),
        UniqueConstraint("task_definition_id", name="uq_task_source_definition"),
        Index("ix_task_source_downloader_id", "downloader_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_definition.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    downloader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("downloader.id", ondelete="SET NULL"), nullable=True
    )
    directory_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskFilter(Base):
    __tablename__ = "task_filter"
    __table_args__ = (
        CheckConstraint("min_size_bytes IS NULL OR min_size_bytes >= 0", name="min_size"),
        CheckConstraint("max_size_bytes IS NULL OR max_size_bytes >= 0", name="max_size"),
        CheckConstraint(
            "min_size_bytes IS NULL OR max_size_bytes IS NULL OR min_size_bytes <= max_size_bytes",
            name="size_range",
        ),
        CheckConstraint("max_scan_depth IS NULL OR max_scan_depth >= 0", name="max_scan_depth"),
        UniqueConstraint("task_definition_id", name="uq_task_filter_definition"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_definition.id", ondelete="CASCADE"), nullable=False
    )
    file_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    video_extensions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    archive_extensions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    min_size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    max_size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    include_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    exclude_names: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    ignore_temp_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    temp_patterns: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    include_subdirectories: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_scan_depth: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskOutputPolicy(Base):
    __tablename__ = "task_output_policy"
    __table_args__ = (
        CheckConstraint(f"storage_mode IN ({_TASK_STORAGE_MODE_SQL})", name="storage_mode"),
        CheckConstraint(
            f"conflict_policy IN ({_TASK_CONFLICT_POLICY_SQL})", name="conflict_policy"
        ),
        UniqueConstraint("task_definition_id", name="uq_task_output_policy_definition"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_definition.id", ondelete="CASCADE"), nullable=False
    )
    output_directory: Mapped[str] = mapped_column(Text, nullable=False)
    storage_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    preserve_structure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    conflict_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskExecutionPolicy(Base):
    __tablename__ = "task_execution_policy"
    __table_args__ = (
        CheckConstraint(f"initial_scope IN ({_TASK_INITIAL_SCOPE_SQL})", name="initial_scope"),
        CheckConstraint(f"overlap_policy IN ({_TASK_OVERLAP_POLICY_SQL})", name="overlap_policy"),
        CheckConstraint("stability_wait_seconds >= 0", name="stability_wait"),
        CheckConstraint("debounce_seconds >= 0", name="debounce"),
        CheckConstraint("max_auto_retries BETWEEN 0 AND 20", name="max_auto_retries"),
        UniqueConstraint("task_definition_id", name="uq_task_execution_policy_definition"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_definition.id", ondelete="CASCADE"), nullable=False
    )
    stability_detection_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stability_wait_seconds: Mapped[int] = mapped_column(nullable=False, default=60)
    only_completed_downloads: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    initial_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    debounce_seconds: Mapped[int] = mapped_column(nullable=False, default=30)
    overlap_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    auto_retry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_auto_retries: Mapped[int] = mapped_column(nullable=False, default=3)
    retry_intervals_seconds: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskExecution(Base):
    __tablename__ = "task_execution"
    __table_args__ = (
        CheckConstraint(f"trigger IN ({_TASK_EXECUTION_TRIGGER_SQL})", name="trigger"),
        CheckConstraint(f"status IN ({_TASK_EXECUTION_STATUS_SQL})", name="status"),
        CheckConstraint(f"phase IN ({_TASK_EXECUTION_PHASE_SQL})", name="phase"),
        CheckConstraint("discovered_count >= 0", name="discovered_count"),
        CheckConstraint("success_count >= 0", name="success_count"),
        CheckConstraint("failed_count >= 0", name="failed_count"),
        CheckConstraint("skipped_count >= 0", name="skipped_count"),
        Index("ix_task_execution_definition_started", "task_definition_id", "started_at"),
        Index("ix_task_execution_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_definition_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_definition.id", ondelete="SET NULL"), nullable=True
    )
    task_name: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    source_execution_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_execution.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    discovered_count: Mapped[int] = mapped_column(nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskExecutionItem(Base):
    __tablename__ = "task_execution_item"
    __table_args__ = (
        CheckConstraint(f"phase IN ({_TASK_EXECUTION_PHASE_SQL})", name="phase"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size"),
        CheckConstraint("progress IS NULL OR (progress >= 0 AND progress <= 100)", name="progress"),
        UniqueConstraint("execution_id", "source_object_key", name="uq_task_execution_item_source"),
        Index("ix_task_execution_item_execution_phase", "execution_id", "phase"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_execution.id", ondelete="CASCADE"), nullable=False
    )
    unpack_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("unpack_task.id", ondelete="SET NULL"), nullable=True
    )
    source_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    progress: Mapped[int | None] = mapped_column(nullable=True)
    result: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    technical_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TaskExecutionEvent(Base):
    __tablename__ = "task_execution_event"
    __table_args__ = (
        Index("ix_task_execution_event_execution_created", "execution_id", "created_at"),
        Index("ix_task_execution_event_code_created", "event_code", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_execution.id", ondelete="CASCADE"), nullable=False
    )
    event_code: Mapped[str] = mapped_column(String(96), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class UnpackTask(Base):
    __tablename__ = "unpack_task"
    __table_args__ = (
        CheckConstraint(f"status IN ({_TASK_STATUS_SQL})", name="status"),
        Index(
            "uq_unpack_task_logical_run",
            "type",
            "source_downloader_id",
            "source_hash",
            "normalized_unit_key",
            "run_number",
            unique=True,
        ),
        Index("ix_unpack_task_status_updated_at", "status", "updated_at"),
        Index("ix_unpack_task_parent_task_id", "parent_task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_downloader_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_unit_key: Mapped[str] = mapped_column(String(512), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_number: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
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


class OperationJournalTombstone(Base):
    __tablename__ = "operation_journal_tombstone"
    __table_args__ = (
        CheckConstraint("final_status IN ('NOOP', 'ROLLED_BACK')", name="final_status"),
        UniqueConstraint(
            "idempotency_key_digest",
            name="uq_operation_journal_tombstone_idempotency_key_digest",
        ),
        Index("ix_operation_journal_tombstone_task_purged", "task_id", "purged_at"),
    )

    journal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("unpack_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False)
    journal_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    original_created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    original_updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    purged_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


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
