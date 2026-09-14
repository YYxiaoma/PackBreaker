"""允许历史扫描显式取消，同时保留当前游标和统计。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_history_scan_cancelled"
down_revision: str | None = "0021_history_episode_grouping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("history_scan") as batch:
        batch.drop_constraint(op.f("ck_history_scan_status"), type_="check")
        batch.create_check_constraint(
            "status",
            "status IN ('READY', 'SCANNING', 'PAUSED', 'CANCELLED', 'DONE')",
        )


def downgrade() -> None:
    op.execute("UPDATE history_scan SET status = 'PAUSED' WHERE status = 'CANCELLED'")
    with op.batch_alter_table("history_scan") as batch:
        batch.drop_constraint(op.f("ck_history_scan_status"), type_="check")
        batch.create_check_constraint(
            "status",
            "status IN ('READY', 'SCANNING', 'PAUSED', 'DONE')",
        )
