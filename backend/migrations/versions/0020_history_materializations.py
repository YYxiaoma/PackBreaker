"""增加历史扫描文件到普通任务的追加式 materialization 记录。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_history_materializations"
down_revision: str | None = "0019_history_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "history_scan_materialization",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scan_id", sa.String(length=36), nullable=False),
        sa.Column("scan_file_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("source_root", sa.Text(), nullable=True),
        sa.Column("normalized_unit_key", sa.String(length=64), nullable=True),
        sa.Column("unit_kind", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('MATERIALIZED', 'SKIPPED')",
            name="status",
        ),
        sa.CheckConstraint(
            "(status = 'MATERIALIZED' AND task_id IS NOT NULL) OR "
            "(status = 'SKIPPED' AND task_id IS NULL)",
            name="task_consistency",
        ),
        sa.ForeignKeyConstraint(["scan_id"], ["history_scan.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scan_file_id"], ["history_scan_file.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["unpack_task.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scan_file_id",
            "snapshot_digest",
            name="uq_history_scan_materialization_file_snapshot",
        ),
    )
    op.create_index(
        "ix_history_scan_materialization_scan_created_at",
        "history_scan_materialization",
        ["scan_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_history_scan_materialization_scan_created_at",
        table_name="history_scan_materialization",
    )
    op.drop_table("history_scan_materialization")
