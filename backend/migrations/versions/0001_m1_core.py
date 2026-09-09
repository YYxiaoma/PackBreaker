"""建立 M1 任务、事件与操作日志核心表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_m1_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATUSES = (
    "PENDING",
    "ANALYZING",
    "SEARCHING",
    "MATCHING",
    "VERIFYING",
    "PREFLIGHT",
    "AWAITING_CONFIRMATION",
    "LINKING",
    "ADDING",
    "CLIENT_VERIFYING",
    "SEEDING",
    "DONE",
    "PAUSED",
    "RETRY",
    "FAILED",
    "CANCELLING",
    "ROLLING_BACK",
    "CANCELLED",
)
OPERATION_STATUSES = (
    "INTENT_RECORDED",
    "APPLIED",
    "NOOP",
    "ROLLBACK_PENDING",
    "ROLLED_BACK",
    "RECONCILE_REQUIRED",
    "ROLLBACK_BLOCKED",
)


def _enum_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "unpack_task",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("source_downloader_id", sa.String(length=36), nullable=False),
        sa.Column("source_hash", sa.String(length=128), nullable=False),
        sa.Column("normalized_unit_key", sa.String(length=512), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
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
        sa.CheckConstraint(_enum_check("status", TASK_STATUSES), name="ck_unpack_task_status"),
        sa.PrimaryKeyConstraint("id", name="pk_unpack_task"),
        sa.UniqueConstraint("idempotency_key", name="uq_unpack_task_idempotency_key"),
    )
    op.create_index(
        "ix_unpack_task_status_updated_at",
        "unpack_task",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "task_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_task_event_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_event"),
    )
    op.create_index(
        "ix_task_event_task_created_at",
        "task_event",
        ["task_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "operation_journal",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("intent", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("before_snapshot", sa.JSON(), nullable=True),
        sa.Column("after_snapshot", sa.JSON(), nullable=True),
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
            _enum_check("status", OPERATION_STATUSES),
            name="ck_operation_journal_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_operation_journal_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operation_journal"),
        sa.UniqueConstraint("idempotency_key", name="uq_operation_journal_idempotency_key"),
    )
    op.create_index(
        "ix_operation_journal_task_status",
        "operation_journal",
        ["task_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_operation_journal_task_status", table_name="operation_journal")
    op.drop_table("operation_journal")
    op.drop_index("ix_task_event_task_created_at", table_name="task_event")
    op.drop_table("task_event")
    op.drop_index("ix_unpack_task_status_updated_at", table_name="unpack_task")
    op.drop_table("unpack_task")
