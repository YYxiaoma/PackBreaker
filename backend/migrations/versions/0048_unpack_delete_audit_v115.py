"""Archive external operations before removing eligible unpack tasks or items."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_unpack_delete_audit_v115"
down_revision: str | None = "0047_unpack_auto_match_enabled_v115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unpack_deleted_item_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("definition_id", sa.String(36), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_unpack_deleted_audit_definition_deleted",
        "unpack_deleted_item_audit",
        ["definition_id", "deleted_at"],
    )
    op.create_index(
        "ix_unpack_deleted_audit_item",
        "unpack_deleted_item_audit",
        ["item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_unpack_deleted_audit_item", table_name="unpack_deleted_item_audit")
    op.drop_index(
        "ix_unpack_deleted_audit_definition_deleted",
        table_name="unpack_deleted_item_audit",
    )
    op.drop_table("unpack_deleted_item_audit")
