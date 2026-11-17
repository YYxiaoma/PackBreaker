"""Track template provenance so official built-ins can be shipped read-only."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_unpack_template_origin"
down_revision: str | None = "0054_unpack_reconciled_operation_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unpack_template",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="USER"),
    )
    op.add_column("unpack_template", sa.Column("builtin_key", sa.String(length=120), nullable=True))
    op.add_column("unpack_template", sa.Column("builtin_version", sa.Integer(), nullable=True))
    op.create_index(
        "uq_unpack_template_builtin_key",
        "unpack_template",
        ["builtin_key"],
        unique=True,
        sqlite_where=sa.text("builtin_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_unpack_template_builtin_key", table_name="unpack_template")
    with op.batch_alter_table("unpack_template") as batch:
        batch.drop_column("builtin_version")
        batch.drop_column("builtin_key")
        batch.drop_column("origin")
