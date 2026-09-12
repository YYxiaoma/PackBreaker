"""增加 operation journal 保留期 tombstone。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_operation_retention"
down_revision: str | None = "0015_notification_subjects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_journal_tombstone",
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("final_status", sa.String(length=32), nullable=False),
        sa.Column("journal_digest", sa.String(length=64), nullable=False),
        sa.Column("original_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "final_status IN ('NOOP', 'ROLLED_BACK')",
            name="final_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_operation_journal_tombstone_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("journal_id", name="pk_operation_journal_tombstone"),
        sa.UniqueConstraint(
            "idempotency_key_digest",
            name="uq_operation_journal_tombstone_idempotency_key_digest",
        ),
    )
    op.create_index(
        "ix_operation_journal_tombstone_task_purged",
        "operation_journal_tombstone",
        ["task_id", "purged_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operation_journal_tombstone_task_purged",
        table_name="operation_journal_tombstone",
    )
    op.drop_table("operation_journal_tombstone")
