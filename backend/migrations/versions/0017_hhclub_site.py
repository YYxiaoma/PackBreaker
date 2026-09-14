"""加入 HHClub 站点类型。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_hhclub_site"
down_revision: str | None = "0016_operation_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("site") as batch_op:
        batch_op.drop_constraint("type", type_="check")
        batch_op.create_check_constraint("type", "type IN ('MTEAM', 'HDTIME', 'HHCLUB')")


def downgrade() -> None:
    op.execute("DELETE FROM site WHERE type = 'HHCLUB'")
    with op.batch_alter_table("site") as batch_op:
        batch_op.drop_constraint("type", type_="check")
        batch_op.create_check_constraint("type", "type IN ('MTEAM', 'HDTIME')")
