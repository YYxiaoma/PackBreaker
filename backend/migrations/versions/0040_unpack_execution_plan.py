"""v1.0.15 数据拆包 v2 冻结执行计划。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_unpack_execution_plan"
down_revision: str | None = "0039_unpack_external_operation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_execution_item",
        sa.Column("execution_plan", sa.JSON(), nullable=True),
    )
    op.add_column(
        "unpack_execution_item",
        sa.Column("execution_plan_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "unpack_execution_item",
        sa.Column("execution_plan_created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("unpack_execution_item") as batch:
        batch.drop_column("execution_plan_created_at")
        batch.drop_column("execution_plan_digest")
        batch.drop_column("execution_plan")
