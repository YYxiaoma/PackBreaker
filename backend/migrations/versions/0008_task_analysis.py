"""增加任务单元与候选分析持久化。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_task_analysis"
down_revision: str | None = "0007_preflight"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_unit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_unit_key", sa.String(length=64), nullable=False),
        sa.Column("source_root", sa.Text(), nullable=False),
        sa.Column("source_inventory_digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_relative_path", sa.Text(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_task_unit_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_unit"),
        sa.UniqueConstraint(
            "task_id",
            "source_inventory_digest",
            "normalized_unit_key",
            name="uq_task_unit_task_inventory_key",
        ),
    )
    op.create_index(
        "ix_task_unit_task_inventory",
        "task_unit",
        ["task_id", "source_inventory_digest"],
        unique=False,
    )

    op.create_table(
        "task_candidate",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("preflight_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_unit_key", sa.String(length=64), nullable=False),
        sa.Column("site_id", sa.String(length=64), nullable=False),
        sa.Column("torrent_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rejected", sa.Boolean(), nullable=False),
        sa.Column("selected_for_verification", sa.Boolean(), nullable=False),
        sa.Column("verification_level", sa.String(length=32), nullable=True),
        sa.Column("metainfo_digest", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["preflight_snapshot_id"],
            ["preflight_snapshot.id"],
            name="fk_task_candidate_preflight_snapshot_id_preflight_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_task_candidate_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_candidate"),
        sa.UniqueConstraint(
            "preflight_snapshot_id",
            "site_id",
            "torrent_id",
            name="uq_task_candidate_snapshot_site_torrent",
        ),
    )
    op.create_index(
        "ix_task_candidate_task_created_at",
        "task_candidate",
        ["task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_candidate_snapshot_score",
        "task_candidate",
        ["preflight_snapshot_id", "score"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_candidate_snapshot_score", table_name="task_candidate")
    op.drop_index("ix_task_candidate_task_created_at", table_name="task_candidate")
    op.drop_table("task_candidate")
    op.drop_index("ix_task_unit_task_inventory", table_name="task_unit")
    op.drop_table("task_unit")
