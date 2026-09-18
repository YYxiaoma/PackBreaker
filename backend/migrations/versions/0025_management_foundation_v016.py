"""增加 v0.1.6 管理体验共享领域与兼容数据结构。

本迁移避免对已有外键下游引用的父表执行 SQLite batch rebuild，防止旧数据
因表重建触发 ON DELETE 行为。复杂代理参数约束由领域层负责校验。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_management_foundation_v016"
down_revision: str | None = "0024_task_center_v015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "administrator",
        sa.Column("username", sa.String(length=80), nullable=False, server_default="admin"),
    )
    op.add_column(
        "administrator",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "administrator", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "administrator", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ux_administrator_username", "administrator", ["username"], unique=True)

    op.add_column(
        "site",
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
    )
    op.add_column(
        "site",
        sa.Column("search_interval_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("site", sa.Column("user_agent", sa.String(length=512), nullable=True))
    op.add_column(
        "site",
        sa.Column(
            "browser_emulation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "site", sa.Column("proxy_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("site", sa.Column("proxy_host", sa.String(length=255), nullable=True))
    op.add_column("site", sa.Column("proxy_port", sa.Integer(), nullable=True))
    op.add_column("site", sa.Column("proxy_username", sa.String(length=255), nullable=True))
    op.add_column("site", sa.Column("proxy_secret_id", sa.String(length=36), nullable=True))

    op.add_column(
        "notification_channel",
        sa.Column("event_types", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "notification_channel",
        sa.Column("proxy_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "notification_channel", sa.Column("proxy_host", sa.String(length=255), nullable=True)
    )
    op.add_column("notification_channel", sa.Column("proxy_port", sa.Integer(), nullable=True))
    op.add_column(
        "notification_channel", sa.Column("proxy_username", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "notification_channel", sa.Column("proxy_secret_id", sa.String(length=36), nullable=True)
    )

    op.create_table(
        "admin_notification",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'ERROR')", name="ck_admin_notification_severity"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_notification"),
        sa.UniqueConstraint("dedup_key", name="uq_admin_notification_dedup_key"),
    )
    op.create_index(
        "ix_admin_notification_read_created",
        "admin_notification",
        ["read_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_notification_read_created", table_name="admin_notification")
    op.drop_table("admin_notification")

    op.drop_column("notification_channel", "proxy_secret_id")
    op.drop_column("notification_channel", "proxy_username")
    op.drop_column("notification_channel", "proxy_port")
    op.drop_column("notification_channel", "proxy_host")
    op.drop_column("notification_channel", "proxy_enabled")
    op.drop_column("notification_channel", "event_types")

    op.drop_column("site", "proxy_secret_id")
    op.drop_column("site", "proxy_username")
    op.drop_column("site", "proxy_port")
    op.drop_column("site", "proxy_host")
    op.drop_column("site", "proxy_enabled")
    op.drop_column("site", "browser_emulation_enabled")
    op.drop_column("site", "user_agent")
    op.drop_column("site", "search_interval_seconds")
    op.drop_column("site", "request_timeout_seconds")

    op.drop_index("ux_administrator_username", table_name="administrator")
    op.drop_column("administrator", "last_login_at")
    op.drop_column("administrator", "password_changed_at")
    op.drop_column("administrator", "must_change_password")
    op.drop_column("administrator", "username")
