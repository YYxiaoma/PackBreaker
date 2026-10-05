"""v1.0.10 CookieCloud Cron 调度与同步可观测性。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_cookiecloud_cron_v1010"
down_revision: str | None = "0033_movie_dedup_v107"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cookiecloud_setting",
        sa.Column(
            "sync_cron_expression",
            sa.String(length=160),
            nullable=False,
            server_default="*/30 * * * *",
        ),
    )
    op.add_column(
        "cookiecloud_setting",
        sa.Column("source_domains", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "cookiecloud_setting",
        sa.Column("source_cookies", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "cookiecloud_setting",
        sa.Column("eligible_sites", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "cookiecloud_setting",
        sa.Column("unchanged_sites", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("cookiecloud_setting") as batch:
        batch.drop_column("unchanged_sites")
        batch.drop_column("eligible_sites")
        batch.drop_column("source_cookies")
        batch.drop_column("source_domains")
        batch.drop_column("sync_cron_expression")
