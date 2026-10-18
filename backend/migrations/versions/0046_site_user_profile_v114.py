"""v1.1.4 站点用户详情快照与同步历史。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_site_user_profile_v114"
down_revision: str | None = "0045_seven_site_capacity_v111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_user_profile",
        sa.Column(
            "site_id",
            sa.String(length=36),
            sa.ForeignKey("site.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("uid", sa.String(length=128), nullable=True),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("user_level", sa.String(length=256), nullable=True),
        sa.Column("join_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_access_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("real_uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("real_downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ratio", sa.Float(), nullable=True),
        sa.Column("torrents_posted", sa.Integer(), nullable=True),
        sa.Column("seeding_count", sa.Integer(), nullable=True),
        sa.Column("seeding_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("bonus", sa.Float(), nullable=True),
        sa.Column("seeding_points", sa.Float(), nullable=True),
        sa.Column("bonus_per_hour", sa.Float(), nullable=True),
        sa.Column("hnr_status", sa.String(length=64), nullable=True),
        sa.Column("hnr_count", sa.Integer(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=True),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "site_user_profile_sync",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "site_id",
            sa.String(length=36),
            sa.ForeignKey("site.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("field_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_site_user_profile_sync_site_started",
        "site_user_profile_sync",
        ["site_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_site_user_profile_sync_site_started", table_name="site_user_profile_sync")
    op.drop_table("site_user_profile_sync")
    op.drop_table("site_user_profile")
