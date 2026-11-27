"""种子收益归因：按下载器种子与自然日记录真实上传量。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_torrent_yield_daily"
down_revision: str | None = "0063_site_statistics_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "torrent_yield_daily",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "downloader_id",
            sa.String(length=36),
            sa.ForeignKey("downloader.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("torrent_hash", sa.String(length=64), nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column(
            "seeding_watch_id",
            sa.String(length=36),
            sa.ForeignKey("seeding_watch.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("site_key", sa.String(length=64), nullable=True),
        sa.Column("remote_torrent_id", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("first_uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("last_uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("upload_delta_bytes", sa.BigInteger(), nullable=True),
        sa.Column("first_seeding_seconds", sa.BigInteger(), nullable=True),
        sa.Column("last_seeding_seconds", sa.BigInteger(), nullable=True),
        sa.Column("seeding_seconds_delta", sa.BigInteger(), nullable=True),
        sa.Column("bonus_contribution", sa.Float(), nullable=True),
        sa.Column("bonus_per_hour_contribution", sa.Float(), nullable=True),
        sa.Column(
            "contribution_source",
            sa.String(length=32),
            nullable=False,
            server_default="NOT_AVAILABLE",
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="INCOMPLETE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "downloader_id",
            "torrent_hash",
            "stat_date",
            name="uq_torrent_yield_daily_observation",
        ),
    )
    op.create_index(
        "ix_torrent_yield_daily_stat_date", "torrent_yield_daily", ["stat_date"], unique=False
    )
    op.create_index(
        "ix_torrent_yield_daily_site_key", "torrent_yield_daily", ["site_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_torrent_yield_daily_site_key", table_name="torrent_yield_daily")
    op.drop_index("ix_torrent_yield_daily_stat_date", table_name="torrent_yield_daily")
    op.drop_table("torrent_yield_daily")
