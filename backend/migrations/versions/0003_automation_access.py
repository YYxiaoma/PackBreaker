"""增加自动化 API Token 表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_api_tokens"
down_revision: str | None = "0002_auth_and_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_token"),
        sa.UniqueConstraint("token_digest", name="uq_api_token_token_digest"),
    )
    op.create_index("ix_api_token_expires_at", "api_token", ["expires_at"], unique=False)
    op.create_index("ix_api_token_revoked_at", "api_token", ["revoked_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_token_revoked_at", table_name="api_token")
    op.drop_index("ix_api_token_expires_at", table_name="api_token")
    op.drop_table("api_token")
