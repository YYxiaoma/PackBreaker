"""站点资产趋势：按应用时区保存站点统计日快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_site_statistics_history"
down_revision: str | None = "0062_candidate_site_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_statistics_history",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "site_id",
            sa.String(length=36),
            sa.ForeignKey("site.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Float(), nullable=True),
        sa.Column("bonus", sa.Float(), nullable=True),
        sa.Column("seeding_points", sa.Float(), nullable=True),
        sa.Column("bonus_per_hour", sa.Float(), nullable=True),
        sa.Column("seeding_count", sa.Integer(), nullable=True),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("seeding_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("site_id", "stat_date", name="uq_site_statistics_history_site_date"),
    )
    op.create_index(
        "ix_site_statistics_history_stat_date",
        "site_statistics_history",
        ["stat_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_site_statistics_history_stat_date", table_name="site_statistics_history")
    op.drop_table("site_statistics_history")
