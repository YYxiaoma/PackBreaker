"""新增默认关闭的计划备份策略与最近执行状态。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_backup_policy"
down_revision: str | None = "0022_history_scan_cancelled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup_policy",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("interval_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("keep_latest", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'default'", name=op.f("ck_backup_policy_singleton")),
        sa.CheckConstraint(
            "interval_hours BETWEEN 1 AND 168", name=op.f("ck_backup_policy_interval_hours")
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 1 AND 3650", name=op.f("ck_backup_policy_retention_days")
        ),
        sa.CheckConstraint(
            "keep_latest BETWEEN 1 AND 100", name=op.f("ck_backup_policy_keep_latest")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_backup_policy_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_policy")),
    )

    op.execute(
        sa.text(
            "INSERT INTO backup_policy "
            "(id, enabled, interval_hours, retention_days, keep_latest, version, "
            "created_at, updated_at) "
            "VALUES ('default', 0, 24, 30, 3, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.drop_table("backup_policy")
