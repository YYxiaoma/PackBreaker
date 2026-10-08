"""v1.0.15 退役旧任务运行时与 Telegram 旧审批开关。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_retire_legacy_task_runtime"
down_revision: str | None = "0043_unpack_match_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE ai_channel_binding "
            "SET approval_enabled = 0, version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE approval_enabled = 1"
        )
    )
    op.drop_index(
        "ix_notification_outbox_task_updated",
        table_name="notification_outbox",
    )
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_notification_outbox_subject_identity"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_notification_outbox_subject_kind"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_notification_outbox_last_event_id_task_event"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("fk_notification_outbox_task_id_unpack_task"),
            type_="foreignkey",
        )
        batch_op.drop_column("last_event_id")
        batch_op.drop_column("task_id")
        batch_op.create_check_constraint(
            "subject_kind",
            "subject_kind IN ('TASK', 'SITE', 'UNPACK')",
        )


def downgrade() -> None:
    # 无法从通用 subject 字段恢复历史 task_event ID，因此降级时仅保留
    # 不依赖旧 task/event 外键的 SITE 通知。旧审批能力也不会自动重新开启。
    connection = op.get_bind()
    connection.execute(sa.text("DELETE FROM notification_outbox WHERE subject_kind != 'SITE'"))
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_notification_outbox_subject_kind"),
            type_="check",
        )
        batch_op.add_column(sa.Column("task_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("last_event_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_notification_outbox_task_id_unpack_task"),
            "unpack_task",
            ["task_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            op.f("fk_notification_outbox_last_event_id_task_event"),
            "task_event",
            ["last_event_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_check_constraint(
            "subject_kind",
            "subject_kind IN ('TASK', 'SITE')",
        )
        batch_op.create_check_constraint(
            "subject_identity",
            "(subject_kind = 'TASK' AND task_id IS NOT NULL AND last_event_id IS NOT NULL "
            "AND subject_id = task_id) OR "
            "(subject_kind = 'SITE' AND task_id IS NULL AND last_event_id IS NULL)",
        )
    op.create_index(
        "ix_notification_outbox_task_updated",
        "notification_outbox",
        ["task_id", "updated_at"],
        unique=False,
    )
