"""为历史剧集 materialization 增加季集归组与多版本证据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_history_episode_grouping"
down_revision: str | None = "0020_history_materializations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("history_scan_materialization") as batch:
        batch.add_column(sa.Column("episode_kind", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("episode_season", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("episode_start", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("episode_end", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("episode_label", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("episode_group_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("episode_variant_key", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_history_scan_materialization_scan_episode_group",
        "history_scan_materialization",
        ["scan_id", "episode_group_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_history_scan_materialization_scan_episode_group",
        table_name="history_scan_materialization",
    )
    with op.batch_alter_table("history_scan_materialization") as batch:
        batch.drop_column("episode_variant_key")
        batch.drop_column("episode_group_key")
        batch.drop_column("episode_label")
        batch.drop_column("episode_end")
        batch.drop_column("episode_start")
        batch.drop_column("episode_season")
        batch.drop_column("episode_kind")
