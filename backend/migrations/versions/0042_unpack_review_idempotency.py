"""v1.0.15 数据拆包 v2 审核幂等键。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_unpack_review_idempotency"
down_revision: str | None = "0041_unpack_execution_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_review_decision",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    op.execute(
        "UPDATE unpack_review_decision "
        "SET idempotency_key = 'legacy-' || id "
        "WHERE idempotency_key IS NULL"
    )
    with op.batch_alter_table("unpack_review_decision") as batch:
        batch.alter_column("idempotency_key", existing_type=sa.String(length=128), nullable=False)
        batch.create_unique_constraint(
            "uq_unpack_review_idempotency_key",
            ["idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("unpack_review_decision") as batch:
        batch.drop_constraint("uq_unpack_review_idempotency_key", type_="unique")
        batch.drop_column("idempotency_key")
