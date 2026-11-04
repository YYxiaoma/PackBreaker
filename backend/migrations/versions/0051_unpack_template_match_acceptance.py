"""Persist user-accepted prior template matches for safe reuse."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_unpack_template_match_acceptance"
down_revision: str | None = "0050_unpack_template_source_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unpack_template_match_acceptance",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "template_id",
            sa.String(36),
            sa.ForeignKey("unpack_template.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_config_id",
            sa.String(36),
            sa.ForeignKey("site.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("site_key", sa.String(64), nullable=False),
        sa.Column("collection_root_name", sa.String(255), nullable=False),
        sa.Column("source_relative_path", sa.Text(), nullable=False),
        sa.Column("source_device", sa.BigInteger(), nullable=False),
        sa.Column("source_inode", sa.BigInteger(), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_mtime_ns", sa.String(32), nullable=False),
        sa.Column("candidate_remote_id", sa.String(128), nullable=False),
        sa.Column("candidate_title", sa.Text(), nullable=False),
        sa.Column("source_execution_id", sa.String(36), nullable=False),
        sa.Column("source_item_id", sa.String(36), nullable=False),
        sa.Column("source_candidate_id", sa.String(36), nullable=False),
        sa.Column("source_template_version", sa.Integer(), nullable=False),
        sa.Column("accepted_score_bps", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column(
            "acceptance_kind",
            sa.String(48),
            nullable=False,
            server_default="USER_CONFIRMED_PREVIOUS_100_PERCENT",
        ),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "template_id",
            "site_config_id",
            "collection_root_name",
            "source_relative_path",
            name="uq_unpack_template_match_acceptance_source",
        ),
        sa.CheckConstraint(
            "accepted_score_bps = 10000", name="ck_unpack_template_match_acceptance_accepted_score"
        ),
    )
    op.create_index(
        "ix_unpack_template_match_acceptance_candidate",
        "unpack_template_match_acceptance",
        ["template_id", "site_config_id", "candidate_remote_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_unpack_template_match_acceptance_candidate",
        table_name="unpack_template_match_acceptance",
    )
    op.drop_table("unpack_template_match_acceptance")
