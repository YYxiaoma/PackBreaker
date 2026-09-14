"""为显式 rerun 增加任务 run lineage。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_task_runs"
down_revision: str | None = "0017_hhclub_site"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 上刻意避免 batch table rebuild：unpack_task 被 task_event / outbox / journal 等
    # 多张表引用，重建父表会让已有 ON DELETE CASCADE 子记录在迁移期间被删除。
    op.add_column("unpack_task", sa.Column("parent_task_id", sa.String(length=36), nullable=True))
    op.add_column(
        "unpack_task",
        sa.Column("run_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.create_index(
        "ix_unpack_task_parent_task_id", "unpack_task", ["parent_task_id"], unique=False
    )
    op.create_index(
        "uq_unpack_task_logical_run",
        "unpack_task",
        [
            "type",
            "source_downloader_id",
            "source_hash",
            "normalized_unit_key",
            "run_number",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_unpack_task_logical_run", table_name="unpack_task")
    op.drop_index("ix_unpack_task_parent_task_id", table_name="unpack_task")
    op.drop_column("unpack_task", "run_number")
    op.drop_column("unpack_task", "parent_task_id")
