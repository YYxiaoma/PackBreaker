"""Track duplicate bytes that are already shared through hardlinks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_movie_dedup_shared_link_bytes"
down_revision: str | None = "0055_unpack_template_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 重复内容里「B 侧文件已有多重硬链接」的部分：删除单路径不释放空间，
    # 单独记账，避免「重复 25G / 预计释放 5G」看起来像算错。
    op.add_column(
        "movie_dedup_job",
        sa.Column("shared_link_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    # 历史任务按已有明细回填，避免升级后老数据显示为 0。
    op.execute(
        sa.text(
            """
            UPDATE movie_dedup_job SET shared_link_bytes = (
                SELECT COALESCE(SUM(inv.allocated_bytes), 0)
                FROM movie_dedup_file_inventory inv
                WHERE inv.link_count > 1
                  AND inv.id IN (
                      SELECT pair.target_file_id FROM movie_dedup_pair pair
                      WHERE pair.job_id = movie_dedup_job.id
                        AND pair.full_hash_match = 1
                  )
            )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("movie_dedup_job") as batch:
        batch.drop_column("shared_link_bytes")
