"""v1.0.15 选定影片持久化完整源文件快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_unpack_selected_snapshot"
down_revision: str | None = "0036_unpack_source_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_definition_selected_source",
        sa.Column(
            "source_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("unpack_definition_selected_source") as batch:
        batch.drop_column("source_snapshot")
