"""Add seeding health monitor tables.

Four tables back the monitoring center: the watch list (one row per torrent in a
downloader), the issue lifecycle, the patrol run log, and per-site H&R policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_seeding_health_monitor"
down_revision: str | None = "0056_movie_dedup_shared_link_bytes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 domain.seeding_health.ACTIVE_ISSUE_STATUSES 保持一致。
_ACTIVE_STATUS_SQL = "status IN ('OPEN', 'ACKED', 'REPAIRING')"


def upgrade() -> None:
    op.create_table(
        "seeding_watch",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("downloader_id", sa.String(length=36), nullable=False),
        sa.Column("torrent_hash", sa.String(length=64), nullable=False),
        sa.Column("torrent_name", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("save_path", sa.String(length=2048), nullable=False),
        sa.Column("content_path", sa.String(length=2048), nullable=True),
        sa.Column("site_key", sa.String(length=64), nullable=True),
        sa.Column("remote_torrent_id", sa.String(length=64), nullable=True),
        sa.Column("identity_source", sa.String(length=16), nullable=True),
        sa.Column("identity_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("seeding_time_seconds", sa.BigInteger(), nullable=True),
        sa.Column("tracker_state", sa.String(length=32), nullable=True),
        sa.Column("tracker_message", sa.String(length=512), nullable=True),
        sa.Column("tracker_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("site_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("site_outcome", sa.String(length=16), nullable=True),
        sa.Column("consecutive_misses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("presence", sa.String(length=16), nullable=False, server_default="ACTIVE"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "identity_source IS NULL OR identity_source IN "
            "('COMMENT', 'PLAN', 'NAME_SIZE', 'MANUAL')",
            name="ck_seeding_watch_identity_source",
        ),
        sa.CheckConstraint("presence IN ('ACTIVE', 'GONE')", name="ck_seeding_watch_presence"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_seeding_watch_size_bytes"),
        sa.CheckConstraint(
            "site_outcome IS NULL OR site_outcome IN ('FOUND', 'MISSING', 'UNKNOWN')",
            name="ck_seeding_watch_site_outcome",
        ),
        sa.CheckConstraint("consecutive_misses >= 0", name="ck_seeding_watch_consecutive_misses"),
        sa.CheckConstraint(
            "seeding_time_seconds IS NULL OR seeding_time_seconds >= 0",
            name="ck_seeding_watch_seeding_time_seconds",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_seeding_watch"),
        sa.ForeignKeyConstraint(
            ["downloader_id"],
            ["downloader.id"],
            name="fk_seeding_watch_downloader_id_downloader",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("downloader_id", "torrent_hash", name="uq_seeding_watch_hash"),
    )
    op.create_index("ix_seeding_watch_site", "seeding_watch", ["site_key", "remote_torrent_id"])
    op.create_index("ix_seeding_watch_presence", "seeding_watch", ["presence", "last_seen_at"])

    op.create_table(
        "seeding_health_issue",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("watch_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("hnr_remaining_hours", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.String(length=32), nullable=False),
        sa.Column("detect_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_reason", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('SITE_MISSING', 'SITE_ONLY', 'TRACKER_ERROR', 'FILE_MISSING')",
            name="ck_seeding_health_issue_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('HIGH', 'MEDIUM', 'LOW')",
            name="ck_seeding_health_issue_severity",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ACKED', 'REPAIRING', 'RESOLVED', 'IGNORED')",
            name="ck_seeding_health_issue_status",
        ),
        sa.CheckConstraint(
            "resolved_reason IS NULL OR resolved_reason IN ('AUTO', 'MANUAL', 'REPAIRED', 'GONE')",
            name="ck_seeding_health_issue_resolved_reason",
        ),
        sa.CheckConstraint(
            "suggestion IN ('RETRIEVE_TORRENT', 'RECHECK', 'RESEED', 'NONE')",
            name="ck_seeding_health_issue_suggestion",
        ),
        sa.CheckConstraint(
            "risk_score BETWEEN 0 AND 100", name="ck_seeding_health_issue_risk_score"
        ),
        sa.CheckConstraint(
            "hnr_remaining_hours IS NULL OR hnr_remaining_hours >= 0",
            name="ck_seeding_health_issue_hnr_remaining_hours",
        ),
        sa.CheckConstraint("detect_count >= 1", name="ck_seeding_health_issue_detect_count"),
        sa.PrimaryKeyConstraint("id", name="pk_seeding_health_issue"),
        sa.ForeignKeyConstraint(
            ["watch_id"],
            ["seeding_watch.id"],
            name="fk_seeding_health_issue_watch_id_seeding_watch",
            ondelete="CASCADE",
        ),
    )
    # 同一对象同类异常只允许一个活跃实例；已解决/已忽略的可以并存。
    op.create_index(
        "uq_seeding_health_issue_active",
        "seeding_health_issue",
        ["watch_id", "kind"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_STATUS_SQL),
    )
    op.create_index(
        "ix_seeding_health_issue_list",
        "seeding_health_issue",
        ["status", "kind", "severity", "last_detected_at"],
    )

    op.create_table(
        "seeding_health_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="RUNNING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scanned_watches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_issues", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_issues", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("degraded_sites", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'OK', 'PARTIAL', 'FAILED', 'CANCELLED')",
            name="ck_seeding_health_run_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('SCHEDULED', 'MANUAL')", name="ck_seeding_health_run_trigger"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_seeding_health_run"),
    )
    op.create_index("ix_seeding_health_run_started", "seeding_health_run", ["started_at"])

    op.create_table(
        "seeding_policy",
        sa.Column("site_key", sa.String(length=64), nullable=False),
        sa.Column("min_seeding_hours", sa.Integer(), nullable=True),
        sa.Column("requires_hr", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(length=12), nullable=False, server_default="DEFAULT"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('DEFAULT', 'MANUAL')", name="ck_seeding_policy_source"),
        sa.CheckConstraint(
            "min_seeding_hours IS NULL OR min_seeding_hours BETWEEN 0 AND 8760",
            name="ck_seeding_policy_min_seeding_hours",
        ),
        sa.PrimaryKeyConstraint("site_key", name="pk_seeding_policy"),
    )


def downgrade() -> None:
    op.drop_table("seeding_policy")
    op.drop_index("ix_seeding_health_run_started", table_name="seeding_health_run")
    op.drop_table("seeding_health_run")
    op.drop_index("ix_seeding_health_issue_list", table_name="seeding_health_issue")
    op.drop_index("uq_seeding_health_issue_active", table_name="seeding_health_issue")
    op.drop_table("seeding_health_issue")
    op.drop_index("ix_seeding_watch_presence", table_name="seeding_watch")
    op.drop_index("ix_seeding_watch_site", table_name="seeding_watch")
    op.drop_table("seeding_watch")
