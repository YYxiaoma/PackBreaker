"""增加下载器配置、能力探测和路径诊断状态。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_downloaders"
down_revision: str | None = "0003_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "downloader",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("secret_id", sa.String(length=36), nullable=True),
        sa.Column("monitor_rules", sa.JSON(), nullable=False),
        sa.Column("path_mappings", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("connection_status", sa.String(length=16), nullable=False),
        sa.Column("path_mapping_status", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_path_diagnostic_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("type IN ('QBITTORRENT', 'TRANSMISSION')", name="ck_downloader_type"),
        sa.ForeignKeyConstraint(
            ["secret_id"], ["secret.id"], name="fk_downloader_secret_id_secret", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_downloader"),
        sa.UniqueConstraint("name", name="uq_downloader_name"),
    )
    op.create_index("ix_downloader_type_enabled", "downloader", ["type", "enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_downloader_type_enabled", table_name="downloader")
    op.drop_table("downloader")
