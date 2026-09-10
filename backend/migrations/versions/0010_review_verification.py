"""增加人工映射重验证不可变证据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_review_verification"
down_revision: str | None = "0009_task_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_review_verification",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_revision_id", sa.String(length=36), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_unit_id", sa.String(length=36), nullable=False),
        sa.Column("preflight_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("source_inventory_digest", sa.String(length=64), nullable=False),
        sa.Column("metainfo_digest", sa.String(length=64), nullable=False),
        sa.Column("verification_level", sa.String(length=32), nullable=False),
        sa.Column("mappings", sa.JSON(), nullable=False),
        sa.Column("verification_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["review_revision_id"],
            ["task_review_revision.id"],
            name="fk_task_review_verification_review_revision_id_task_review_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_task_review_verification_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_unit_id"],
            ["task_unit.id"],
            name="fk_task_review_verification_task_unit_id_task_unit",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preflight_snapshot_id"],
            ["preflight_snapshot.id"],
            name="fk_task_review_verification_preflight_snapshot_id_preflight_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["task_candidate.id"],
            name="fk_task_review_verification_candidate_id_task_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_review_verification"),
        sa.UniqueConstraint(
            "review_revision_id",
            name="uq_task_review_verification_revision",
        ),
        sa.UniqueConstraint(
            "verification_digest",
            name="uq_task_review_verification_verification_digest",
        ),
    )
    op.create_index(
        "ix_task_review_verification_task_created_at",
        "task_review_verification",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_review_verification_task_created_at",
        table_name="task_review_verification",
    )
    op.drop_table("task_review_verification")
