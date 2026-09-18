"""增加 v0.1.8 风险摘要、审批状态与监控任务高风险预授权策略。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_task_approval_v018"
down_revision: str | None = "0026_ai_agent_v016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_execution_policy") as batch:
        batch.add_column(
            sa.Column(
                "high_risk_preauthorization_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "high_risk_allowed_action_kinds",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )

    op.create_table(
        "task_risk_summary",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_unit_id", sa.String(length=36), nullable=False),
        sa.Column("execution_plan_id", sa.String(length=36), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("action_kinds", sa.JSON(), nullable=False),
        sa.Column("hardlink_count", sa.Integer(), nullable=False),
        sa.Column("client_fetch_count", sa.Integer(), nullable=False),
        sa.Column("create_directory_count", sa.Integer(), nullable=False),
        sa.Column("estimated_download_bytes_upper_bound", sa.BigInteger(), nullable=False),
        sa.Column("risk_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["unpack_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_unit_id"], ["task_unit.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_plan_id"], ["task_execution_plan.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_plan_id", name="uq_task_risk_summary_execution_plan"),
        sa.UniqueConstraint("risk_digest", name="uq_task_risk_summary_risk_digest"),
    )
    op.create_index(
        "ix_task_risk_summary_task_created",
        "task_risk_summary",
        ["task_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "task_approval",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_unit_id", sa.String(length=36), nullable=False),
        sa.Column("execution_plan_id", sa.String(length=36), nullable=False),
        sa.Column("risk_summary_id", sa.String(length=36), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("decision_source", sa.String(length=24), nullable=True),
        sa.Column("actor_kind", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="ck_task_approval_state",
        ),
        sa.CheckConstraint(
            "decision_source IS NULL OR decision_source IN ('WEB', 'PREAUTHORIZED')",
            name="ck_task_approval_decision_source",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["unpack_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_unit_id"], ["task_unit.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_plan_id"], ["task_execution_plan.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["risk_summary_id"], ["task_risk_summary.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_plan_id", name="uq_task_approval_execution_plan"),
    )
    op.create_index(
        "ix_task_approval_task_state",
        "task_approval",
        ["task_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_approval_task_state", table_name="task_approval")
    op.drop_table("task_approval")
    op.drop_index("ix_task_risk_summary_task_created", table_name="task_risk_summary")
    op.drop_table("task_risk_summary")
    with op.batch_alter_table("task_execution_policy") as batch:
        batch.drop_column("high_risk_allowed_action_kinds")
        batch.drop_column("high_risk_preauthorization_enabled")
