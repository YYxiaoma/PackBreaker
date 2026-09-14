"""增加 M5 历史目录扫描与增量文件快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_history_scans"
down_revision: str | None = "0018_task_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "history_scan",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("root_relative_path", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.String(length=16), nullable=False),
        sa.Column("extensions", sa.JSON(), nullable=False),
        sa.Column("exclude_patterns", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("changed_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("media_kind IN ('MOVIE', 'EPISODE')", name="media_kind"),
        sa.CheckConstraint("status IN ('READY', 'SCANNING', 'PAUSED', 'DONE')", name="status"),
        sa.CheckConstraint("generation >= 0", name="generation_nonnegative"),
        sa.CheckConstraint("discovered_count >= 0", name="discovered_count_nonnegative"),
        sa.CheckConstraint("new_count >= 0", name="new_count_nonnegative"),
        sa.CheckConstraint("changed_count >= 0", name="changed_count_nonnegative"),
        sa.CheckConstraint("unchanged_count >= 0", name="unchanged_count_nonnegative"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "root_relative_path",
            "media_kind",
            name="uq_history_scan_root_media_kind",
        ),
    )
    op.create_index(
        "ix_history_scan_status_updated_at",
        "history_scan",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "history_scan_file",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scan_id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("device", sa.Integer(), nullable=False),
        sa.Column("inode", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.Integer(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("last_seen_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scan_id"], ["history_scan.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scan_id", "relative_path", name="uq_history_scan_file_scan_path"),
    )
    op.create_index(
        "ix_history_scan_file_scan_generation",
        "history_scan_file",
        ["scan_id", "last_seen_generation"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_history_scan_file_scan_generation", table_name="history_scan_file")
    op.drop_table("history_scan_file")
    op.drop_index("ix_history_scan_status_updated_at", table_name="history_scan")
    op.drop_table("history_scan")
