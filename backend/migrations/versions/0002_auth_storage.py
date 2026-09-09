"""增加管理员认证、持久会话与 secret store 表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_auth_and_secrets"
down_revision: str | None = "0001_m1_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "administrator",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("id = 'admin'", name="ck_administrator_singleton"),
        sa.PrimaryKeyConstraint("id", name="pk_administrator"),
    )

    op.create_table(
        "admin_session",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("administrator_id", sa.String(length=16), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["administrator_id"],
            ["administrator.id"],
            name="fk_admin_session_administrator_id_administrator",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_session"),
        sa.UniqueConstraint("token_digest", name="uq_admin_session_token_digest"),
    )
    op.create_index(
        "ix_admin_session_expires_at",
        "admin_session",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "secret",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_secret"),
    )
    op.create_index("ix_secret_kind", "secret", ["kind"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_secret_kind", table_name="secret")
    op.drop_table("secret")
    op.drop_index("ix_admin_session_expires_at", table_name="admin_session")
    op.drop_table("admin_session")
    op.drop_table("administrator")
