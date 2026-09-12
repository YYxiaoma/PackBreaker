"""泛化通知 outbox subject 以支持站点可靠性聚合通知。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_notification_subjects"
down_revision: str | None = "0014_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox", sa.Column("subject_kind", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "notification_outbox", sa.Column("subject_id", sa.String(length=128), nullable=True)
    )
    op.execute("UPDATE notification_outbox SET subject_kind = 'TASK', subject_id = task_id")
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint("uq_notification_outbox_channel_task_event_key", type_="unique")
        batch_op.alter_column("task_id", existing_type=sa.String(length=36), nullable=True)
        batch_op.alter_column("last_event_id", existing_type=sa.String(length=36), nullable=True)
        batch_op.alter_column("subject_kind", existing_type=sa.String(length=16), nullable=False)
        batch_op.alter_column("subject_id", existing_type=sa.String(length=128), nullable=False)
        batch_op.create_check_constraint("subject_kind", "subject_kind IN ('TASK', 'SITE')")
        batch_op.create_check_constraint(
            "subject_identity",
            "(subject_kind = 'TASK' AND task_id IS NOT NULL AND last_event_id IS NOT NULL "
            "AND subject_id = task_id) OR "
            "(subject_kind = 'SITE' AND task_id IS NULL AND last_event_id IS NULL)",
        )
        batch_op.create_unique_constraint(
            "uq_notification_outbox_channel_subject_event_key",
            ["channel_id", "subject_kind", "subject_id", "event_key"],
        )
    op.create_index(
        "ix_notification_outbox_subject_updated",
        "notification_outbox",
        ["subject_kind", "subject_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.execute("DELETE FROM notification_outbox WHERE subject_kind != 'TASK'")
    op.drop_index("ix_notification_outbox_subject_updated", table_name="notification_outbox")
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint("uq_notification_outbox_channel_subject_event_key", type_="unique")
        batch_op.drop_constraint("ck_notification_outbox_subject_identity", type_="check")
        batch_op.drop_constraint("ck_notification_outbox_subject_kind", type_="check")
        batch_op.alter_column("task_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.alter_column("last_event_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.drop_column("subject_id")
        batch_op.drop_column("subject_kind")
        batch_op.create_unique_constraint(
            "uq_notification_outbox_channel_task_event_key",
            ["channel_id", "task_id", "event_key"],
        )
