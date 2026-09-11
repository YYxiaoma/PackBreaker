"""增加通知渠道与可靠投递 outbox。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_notifications"
down_revision: str | None = "0013_task_action_receipt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_channel",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("secret_id", sa.String(length=36), nullable=True),
        sa.Column("task_link_base_url", sa.Text(), nullable=True),
        sa.Column("aggregation_window_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column(
            "connection_status", sa.String(length=16), nullable=False, server_default="UNTESTED"
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.CheckConstraint(
            "type IN ('TELEGRAM', 'SERVERCHAN')", name="ck_notification_channel_type"
        ),
        sa.CheckConstraint(
            "connection_status IN ('UNTESTED', 'OK', 'FAILED')",
            name="ck_notification_channel_connection_status",
        ),
        sa.CheckConstraint(
            "aggregation_window_seconds BETWEEN 1 AND 86400",
            name="ck_notification_channel_aggregation_window_seconds",
        ),
        sa.ForeignKeyConstraint(
            ["secret_id"],
            ["secret.id"],
            name="fk_notification_channel_secret_id_secret",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_channel"),
        sa.UniqueConstraint("name", name="uq_notification_channel_name"),
    )
    op.create_index(
        "ix_notification_channel_type_enabled",
        "notification_channel",
        ["type", "enabled"],
        unique=False,
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("last_event_id", sa.String(length=36), nullable=False),
        sa.Column("channel_version", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "state IN ('PENDING', 'RETRY', 'DELIVERED', 'DEAD')",
            name="ck_notification_outbox_state",
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'ERROR')", name="ck_notification_outbox_severity"
        ),
        sa.CheckConstraint("pending_count >= 0", name="ck_notification_outbox_pending_count"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_outbox_attempt_count"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channel.id"],
            name="fk_notification_outbox_channel_id_notification_channel",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["unpack_task.id"],
            name="fk_notification_outbox_task_id_unpack_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_event_id"],
            ["task_event.id"],
            name="fk_notification_outbox_last_event_id_task_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_outbox"),
        sa.UniqueConstraint(
            "channel_id",
            "task_id",
            "event_key",
            name="uq_notification_outbox_channel_task_event_key",
        ),
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["state", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_task_updated",
        "notification_outbox",
        ["task_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_task_updated", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_due", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index("ix_notification_channel_type_enabled", table_name="notification_channel")
    op.drop_table("notification_channel")
