"""v1.0.15 监控拆包调度时间字段。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_unpack_monitor_schedule"
down_revision: str | None = "0037_unpack_selected_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_definition",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "unpack_definition",
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_unpack_definition_status_next_run",
        "unpack_definition",
        ["status", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_unpack_definition_status_next_run",
        table_name="unpack_definition",
    )
    with op.batch_alter_table("unpack_definition") as batch:
        batch.drop_column("last_triggered_at")
        batch.drop_column("next_run_at")
