"""增加站点配置与只读连接探测状态。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_sites"
down_revision: str | None = "0004_downloaders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("secret_id", sa.String(length=36), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("connection_status", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("type IN ('MTEAM')", name="ck_site_type"),
        sa.ForeignKeyConstraint(
            ["secret_id"], ["secret.id"], name="fk_site_secret_id_secret", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_site"),
        sa.UniqueConstraint("name", name="uq_site_name"),
    )
    op.create_index("ix_site_type_enabled", "site", ["type", "enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_site_type_enabled", table_name="site")
    op.drop_table("site")
