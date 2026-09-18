"""增加 v0.1.8 Telegram 高风险审批配置与投递证据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_telegram_approval_v018"
down_revision: str | None = "0027_task_approval_v018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_channel_binding") as batch:
        batch.add_column(
            sa.Column(
                "approval_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("task_approval") as batch:
        batch.add_column(
            sa.Column("telegram_notified_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("telegram_chat_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("telegram_message_id", sa.String(length=64), nullable=True))
        batch.drop_constraint("ck_task_approval_state", type_="check")
        batch.create_check_constraint(
            "ck_task_approval_state",
            "state IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')",
        )
        batch.drop_constraint("ck_task_approval_decision_source", type_="check")
        batch.create_check_constraint(
            "ck_task_approval_decision_source",
            ("decision_source IS NULL OR decision_source IN ('WEB', 'PREAUTHORIZED', 'TELEGRAM')"),
        )


def downgrade() -> None:
    with op.batch_alter_table("task_approval") as batch:
        batch.drop_constraint("ck_task_approval_decision_source", type_="check")
        batch.create_check_constraint(
            "ck_task_approval_decision_source",
            "decision_source IS NULL OR decision_source IN ('WEB', 'PREAUTHORIZED')",
        )
        batch.drop_constraint("ck_task_approval_state", type_="check")
        batch.create_check_constraint(
            "ck_task_approval_state",
            "state IN ('PENDING', 'APPROVED', 'REJECTED')",
        )
        batch.drop_column("telegram_message_id")
        batch.drop_column("telegram_chat_id")
        batch.drop_column("telegram_notified_at")

    with op.batch_alter_table("ai_channel_binding") as batch:
        batch.drop_column("approval_enabled")
