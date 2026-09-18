"""增加 v0.1.6 AI Agent 配置、Telegram 绑定和有界会话表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_ai_agent_v016"
down_revision: str | None = "0025_management_foundation_v016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_agent_setting",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_secret_id", sa.String(length=36), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_context_messages", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("data_scopes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("connection_status", sa.String(length=16), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("id = 'default'", name="ck_ai_agent_setting_singleton"),
        sa.CheckConstraint(
            "provider_kind IN ('OPENAI', 'OPENAI_COMPATIBLE')",
            name="ck_ai_agent_setting_provider_kind",
        ),
        sa.CheckConstraint(
            "connection_status IN ('UNTESTED', 'OK', 'FAILED')",
            name="ck_ai_agent_setting_connection_status",
        ),
        sa.CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 120",
            name="ck_ai_agent_setting_request_timeout",
        ),
        sa.CheckConstraint(
            "max_context_messages BETWEEN 2 AND 100",
            name="ck_ai_agent_setting_max_context_messages",
        ),
        sa.CheckConstraint("version >= 1", name="ck_ai_agent_setting_version"),
        sa.ForeignKeyConstraint(
            ["api_key_secret_id"],
            ["secret.id"],
            name="fk_ai_agent_setting_api_key_secret_id_secret",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_agent_setting"),
    )

    op.create_table(
        "ai_channel_binding",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("notification_channel_id", sa.String(length=36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_chat_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("allowed_user_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("idle_timeout_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("max_context_messages", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False, server_default="0"),
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
        sa.CheckConstraint("kind = 'TELEGRAM'", name="ck_ai_channel_binding_kind"),
        sa.CheckConstraint(
            "idle_timeout_minutes BETWEEN 5 AND 10080",
            name="ck_ai_channel_binding_idle_timeout",
        ),
        sa.CheckConstraint(
            "max_context_messages BETWEEN 2 AND 100",
            name="ck_ai_channel_binding_max_context_messages",
        ),
        sa.CheckConstraint("last_update_id >= 0", name="ck_ai_channel_binding_last_update_id"),
        sa.CheckConstraint("version >= 1", name="ck_ai_channel_binding_version"),
        sa.ForeignKeyConstraint(
            ["notification_channel_id"],
            ["notification_channel.id"],
            name="fk_ai_channel_binding_notification_channel_id_notification_channel",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_channel_binding"),
        sa.UniqueConstraint(
            "notification_channel_id", name="uq_ai_channel_binding_notification_channel_id"
        ),
    )
    op.create_index(
        "ix_ai_channel_binding_enabled", "ai_channel_binding", ["enabled"], unique=False
    )

    op.create_table(
        "ai_conversation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["ai_channel_binding.id"],
            name="fk_ai_conversation_binding_id_ai_channel_binding",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_conversation"),
    )
    op.create_index(
        "ix_ai_conversation_binding_last_message",
        "ai_conversation",
        ["binding_id", "last_message_at"],
        unique=False,
    )

    op.create_table(
        "ai_message",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("telegram_message_id", sa.String(length=64), nullable=True),
        sa.Column("tool_summary", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("role IN ('USER', 'ASSISTANT')", name="ck_ai_message_role"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["ai_conversation.id"],
            name="fk_ai_message_conversation_id_ai_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_message"),
    )
    op.create_index(
        "ix_ai_message_conversation_created",
        "ai_message",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_message_conversation_created", table_name="ai_message")
    op.drop_table("ai_message")
    op.drop_index("ix_ai_conversation_binding_last_message", table_name="ai_conversation")
    op.drop_table("ai_conversation")
    op.drop_index("ix_ai_channel_binding_enabled", table_name="ai_channel_binding")
    op.drop_table("ai_channel_binding")
    op.drop_table("ai_agent_setting")
