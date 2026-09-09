"""增加不可变 preflight snapshot 证据表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_preflight"
down_revision: str | None = "0006_site_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preflight_snapshot",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("normalized_unit_key", sa.String(length=512), nullable=False),
        sa.Column("source_inventory_digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_preflight_snapshot_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_preflight_snapshot"),
        sa.UniqueConstraint("snapshot_digest", name="uq_preflight_snapshot_snapshot_digest"),
    )
    op.create_index(
        "ix_preflight_snapshot_task_created_at",
        "preflight_snapshot",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_preflight_snapshot_task_created_at", table_name="preflight_snapshot")
    op.drop_table("preflight_snapshot")
