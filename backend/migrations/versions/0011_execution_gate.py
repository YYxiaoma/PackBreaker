"""增加只读 pre-execution gate 不可变证据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_execution_gate"
down_revision: str | None = "0010_review_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_execution_gate",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_unit_id", sa.String(length=36), nullable=False),
        sa.Column("preflight_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("review_revision_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("review_verification_id", sa.String(length=36), nullable=True),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("client_check_required", sa.Boolean(), nullable=False),
        sa.Column("verification_level", sa.String(length=32), nullable=True),
        sa.Column("metainfo_digest", sa.String(length=64), nullable=True),
        sa.Column("blocked_reasons", sa.JSON(), nullable=False),
        sa.Column("gate_digest", sa.String(length=64), nullable=False),
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
            name="fk_task_execution_gate_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_unit_id"],
            ["task_unit.id"],
            name="fk_task_execution_gate_task_unit_id_task_unit",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preflight_snapshot_id"],
            ["preflight_snapshot.id"],
            name="fk_task_execution_gate_preflight_snapshot_id_preflight_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_revision_id"],
            ["task_review_revision.id"],
            name="fk_task_execution_gate_review_revision_id_task_review_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["task_candidate.id"],
            name="fk_task_execution_gate_candidate_id_task_candidate",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["review_verification_id"],
            ["task_review_verification.id"],
            name="fk_task_execution_gate_review_verification_id_task_review_verification",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_execution_gate"),
        sa.UniqueConstraint("gate_digest", name="uq_task_execution_gate_gate_digest"),
    )
    op.create_index(
        "ix_task_execution_gate_unit_created_at",
        "task_execution_gate",
        ["task_unit_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_execution_gate_task_eligible",
        "task_execution_gate",
        ["task_id", "eligible"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_execution_gate_task_eligible", table_name="task_execution_gate")
    op.drop_index("ix_task_execution_gate_unit_created_at", table_name="task_execution_gate")
    op.drop_table("task_execution_gate")
