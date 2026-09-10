"""增加不可变人工审核 revision。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_task_review"
down_revision: str | None = "0008_task_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_review_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_unit_id", sa.String(length=36), nullable=False),
        sa.Column("preflight_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("approved_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("rejected_candidate_ids", sa.JSON(), nullable=False),
        sa.Column("manual_mappings", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("requires_reverification", sa.Boolean(), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("version >= 1", name="ck_task_review_revision_version_positive"),
        sa.ForeignKeyConstraint(
            ["approved_candidate_id"],
            ["task_candidate.id"],
            name="fk_task_review_revision_approved_candidate_id_task_candidate",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["preflight_snapshot_id"],
            ["preflight_snapshot.id"],
            name="fk_task_review_revision_preflight_snapshot_id_preflight_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_task_review_revision_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_unit_id"],
            ["task_unit.id"],
            name="fk_task_review_revision_task_unit_id_task_unit",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_review_revision"),
        sa.UniqueConstraint(
            "task_unit_id",
            "preflight_snapshot_id",
            "version",
            name="uq_task_review_unit_snapshot_version",
        ),
    )
    op.create_index(
        "ix_task_review_task_created_at",
        "task_review_revision",
        ["task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_review_unit_snapshot_version",
        "task_review_revision",
        ["task_unit_id", "preflight_snapshot_id", "version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_review_unit_snapshot_version", table_name="task_review_revision")
    op.drop_index("ix_task_review_task_created_at", table_name="task_review_revision")
    op.drop_table("task_review_revision")
