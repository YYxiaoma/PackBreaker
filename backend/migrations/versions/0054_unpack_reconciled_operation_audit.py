"""Add the reconciled-operation audit table for controlled unpack reconciliation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_unpack_reconciled_operation_audit"
down_revision: str | None = "0053_unpack_sampled_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unpack_reconciled_operation_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("definition_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("status_before", sa.String(length=32), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unpack_reconciled_operation_audit")),
    )
    op.create_index(
        "ix_unpack_reconciled_audit_item",
        "unpack_reconciled_operation_audit",
        ["item_id"],
    )
    op.create_index(
        "ix_unpack_reconciled_audit_execution",
        "unpack_reconciled_operation_audit",
        ["execution_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_unpack_reconciled_audit_execution",
        table_name="unpack_reconciled_operation_audit",
    )
    op.drop_index(
        "ix_unpack_reconciled_audit_item",
        table_name="unpack_reconciled_operation_audit",
    )
    op.drop_table("unpack_reconciled_operation_audit")
