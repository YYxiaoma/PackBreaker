"""v1.0.15 数据拆包 v2 匹配来源持久化。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_unpack_match_origin"
down_revision: str | None = "0042_unpack_review_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_execution_item",
        sa.Column("match_origin", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("unpack_execution_item") as batch:
        batch.drop_column("match_origin")
