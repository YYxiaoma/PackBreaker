"""v1.0.15 数据拆包 v2 执行 checkpoint。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_unpack_execution_state"
down_revision: str | None = "0040_unpack_execution_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_execution_item",
        sa.Column("execution_state", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("unpack_execution_item") as batch:
        batch.drop_column("execution_state")
