"""收口 v0.1.8 升级兼容：安全复用既有 Telegram 通知渠道。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_v018_compatibility"
down_revision: str | None = "0028_telegram_approval_v018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    existing = connection.scalar(
        sa.text("SELECT COUNT(*) FROM ai_channel_binding WHERE id = 'telegram'")
    )
    if int(existing or 0) > 0:
        return

    telegram_channels = tuple(
        connection.execute(
            sa.text(
                "SELECT id FROM notification_channel "
                "WHERE type = 'TELEGRAM' "
                "ORDER BY enabled DESC, created_at, id"
            )
        ).scalars()
    )
    channel_id = telegram_channels[0] if len(telegram_channels) == 1 else None
    connection.execute(
        sa.text(
            "INSERT INTO ai_channel_binding "
            "(id, kind, notification_channel_id, enabled, approval_enabled, "
            "allowed_chat_ids, allowed_user_ids, idle_timeout_minutes, "
            "max_context_messages, last_update_id, version, created_at, updated_at) "
            "VALUES ('telegram', 'TELEGRAM', :channel_id, 0, 0, '[]', '[]', "
            "60, 20, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"channel_id": channel_id},
    )


def downgrade() -> None:
    # 数据兼容迁移不删除 ai_channel_binding，避免降级时误删用户随后修改的 Telegram 配置。
    pass
