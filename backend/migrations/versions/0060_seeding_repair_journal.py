"""add seeding repair operation journal

Revision ID: 0060_seeding_repair_journal
Revises: 0059_seeding_site_only_watch
Create Date: 2026-11-21

Durable intent journal for the two repair actions. Follows the unpack safety
pattern: record intent first, then issue the external write, then reconcile the
observed state. Never inline the write without an intent record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060_seeding_repair_journal"
down_revision: str | None = "0059_seeding_site_only_watch"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_STATUSES = (
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
        "seeding_repair_journal",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation_type", sa.String(length=24), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("before_snapshot", sa.Text(), nullable=True),
        sa.Column("after_snapshot", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation_type IN ('RETRIEVE_TORRENT', 'RESEED')",
            name="ck_seeding_repair_operation_type",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{item}'" for item in _STATUSES) + ")",
            name="ck_seeding_repair_status",
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["seeding_health_issue.id"],
            name="fk_seeding_repair_issue",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_seeding_repair_journal"),
        sa.UniqueConstraint("idempotency_key", name="uq_seeding_repair_idempotency_key"),
    )
    op.create_index("ix_seeding_repair_issue", "seeding_repair_journal", ["issue_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_seeding_repair_issue", table_name="seeding_repair_journal")
    op.drop_table("seeding_repair_journal")
