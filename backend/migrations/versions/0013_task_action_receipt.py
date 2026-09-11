"""增加公开任务动作的持久化幂等与审计 receipt。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_task_action_receipt"
down_revision: str | None = "0012_execution_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_action_receipt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("error_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'SUCCEEDED', 'FAILED')",
            name="ck_task_action_receipt_state",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_task_action_receipt_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_action_receipt"),
        sa.UniqueConstraint(
            "actor_kind",
            "actor_id",
            "idempotency_key_digest",
            name="uq_task_action_receipt_actor_key",
        ),
    )
    op.create_index(
        "ix_task_action_receipt_task_created_at",
        "task_action_receipt",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_action_receipt_task_created_at", table_name="task_action_receipt")
    op.drop_table("task_action_receipt")
