"""v1.0.15 数据拆包 v2 外部副作用 journal。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_unpack_external_operation"
down_revision: str | None = "0038_unpack_monitor_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATION_STATUSES = (
    "INTENT_RECORDED",
    "APPLIED",
    "NOOP",
    "ROLLBACK_PENDING",
    "ROLLED_BACK",
    "RECONCILE_REQUIRED",
    "ROLLBACK_BLOCKED",
)


def upgrade() -> None:
    op.create_table(
        "unpack_external_operation_journal",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("intent", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("before_snapshot", sa.JSON(), nullable=True),
        sa.Column("after_snapshot", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(repr(value) for value in _OPERATION_STATUSES) + ")",
            name="ck_unpack_external_operation_journal_status",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["unpack_execution_item.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_unpack_external_operation_key",
        ),
    )
    op.create_index(
        "ix_unpack_external_operation_item_status",
        "unpack_external_operation_journal",
        ["item_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_unpack_external_operation_item_status",
        table_name="unpack_external_operation_journal",
    )
    op.drop_table("unpack_external_operation_journal")
