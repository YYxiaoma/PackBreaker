"""增加 v1.0.5 CookieCloud 单例配置。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_cookiecloud_v105"
down_revision: str | None = "0031_site_type_capacity_v101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cookiecloud_setting",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("server_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("uuid", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("password_secret_id", sa.String(length=36), nullable=True),
        sa.Column("auto_sync", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column(
            "connection_status",
            sa.String(length=16),
            nullable=False,
            server_default="UNTESTED",
        ),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(length=16), nullable=False, server_default="NEVER"),
        sa.Column("last_sync_error_code", sa.String(length=64), nullable=True),
        sa.Column("matched_sites", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_sites", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unmatched_domains", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.CheckConstraint("id = 'default'", name="ck_cookiecloud_setting_singleton"),
        sa.CheckConstraint(
            "sync_interval_minutes BETWEEN 5 AND 10080",
            name="ck_cookiecloud_setting_sync_interval",
        ),
        sa.CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 120",
            name="ck_cookiecloud_setting_request_timeout",
        ),
        sa.CheckConstraint(
            "connection_status IN ('UNTESTED', 'OK', 'FAILED')",
            name="ck_cookiecloud_setting_connection_status",
        ),
        sa.CheckConstraint(
            "last_sync_status IN ('NEVER', 'SUCCESS', 'FAILED')",
            name="ck_cookiecloud_setting_last_sync_status",
        ),
        sa.CheckConstraint(
            "matched_sites >= 0",
            name="ck_cookiecloud_setting_matched_sites_nonnegative",
        ),
        sa.CheckConstraint(
            "updated_sites >= 0",
            name="ck_cookiecloud_setting_updated_sites_nonnegative",
        ),
        sa.CheckConstraint(
            "unmatched_domains >= 0",
            name="ck_cookiecloud_setting_unmatched_domains_nonnegative",
        ),
        sa.CheckConstraint("version >= 1", name="ck_cookiecloud_setting_version"),
        sa.ForeignKeyConstraint(
            ["password_secret_id"],
            ["secret.id"],
            name="fk_cookiecloud_setting_password_secret_id_secret",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cookiecloud_setting"),
    )


def downgrade() -> None:
    op.drop_table("cookiecloud_setting")
