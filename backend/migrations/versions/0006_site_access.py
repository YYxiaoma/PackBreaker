"""泛化站点凭证类型并加入 HDTime。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_site_credentials"
down_revision: str | None = "0005_sites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("site", sa.Column("credential_kind", sa.String(length=32), nullable=True))
    op.execute("UPDATE site SET credential_kind = 'API_KEY'")
    with op.batch_alter_table("site") as batch_op:
        batch_op.alter_column(
            "credential_kind",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.drop_constraint("ck_site_type", type_="check")
        batch_op.create_check_constraint("type", "type IN ('MTEAM', 'HDTIME')")
        batch_op.create_check_constraint(
            "credential_kind",
            "credential_kind IN ('API_KEY', 'COOKIE')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM site WHERE type != 'MTEAM'")
    with op.batch_alter_table("site") as batch_op:
        batch_op.drop_constraint("ck_site_credential_kind", type_="check")
        batch_op.drop_constraint("ck_site_type", type_="check")
        batch_op.create_check_constraint("type", "type IN ('MTEAM')")
        batch_op.drop_column("credential_kind")
