"""增加无副作用执行计划快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_execution_plan"
down_revision: str | None = "0011_execution_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_execution_plan",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_unit_id", sa.String(length=36), nullable=False),
        sa.Column("execution_gate_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("target_root", sa.Text(), nullable=False),
        sa.Column("target_device", sa.Integer(), nullable=False),
        sa.Column("verification_level", sa.String(length=32), nullable=False),
        sa.Column("client_check_required", sa.Boolean(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("blocked_reasons", sa.JSON(), nullable=False),
        sa.Column("estimated_download_bytes_upper_bound", sa.Integer(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
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
            name="fk_task_execution_plan_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_unit_id"],
            ["task_unit.id"],
            name="fk_task_execution_plan_task_unit_id_task_unit",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_gate_id"],
            ["task_execution_gate.id"],
            name="fk_task_execution_plan_execution_gate_id_task_execution_gate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["task_candidate.id"],
            name="fk_task_execution_plan_candidate_id_task_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_execution_plan"),
        sa.UniqueConstraint("plan_digest", name="uq_task_execution_plan_plan_digest"),
    )
    op.create_index(
        "ix_task_execution_plan_unit_created_at",
        "task_execution_plan",
        ["task_unit_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_execution_plan_task_ready",
        "task_execution_plan",
        ["task_id", "ready"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_execution_plan_task_ready", table_name="task_execution_plan")
    op.drop_index("ix_task_execution_plan_unit_created_at", table_name="task_execution_plan")
    op.drop_table("task_execution_plan")
