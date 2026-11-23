"""allow PRODUCT subject kind in the notification outbox

Revision ID: 0061_notification_product_events
Revises: 0060_seeding_repair_journal
Create Date: 2026-11-23

产品事件（登录、改密、新增下载器/站点、版本更新）此前只存在于事件枚举里，
没有任何生产者。现在它们会真正投影到外发队列，需要一个不绑定任务/站点的
subject 类别。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_notification_product_events"
down_revision: str | None = "0060_seeding_repair_journal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_notification_outbox_subject_kind"),
            type_="check",
        )
        batch_op.create_check_constraint(
            "subject_kind",
            "subject_kind IN ('TASK', 'SITE', 'UNPACK', 'PRODUCT')",
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM notification_outbox WHERE subject_kind = 'PRODUCT'"))
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_notification_outbox_subject_kind"),
            type_="check",
        )
        batch_op.create_check_constraint(
            "subject_kind",
            "subject_kind IN ('TASK', 'SITE', 'UNPACK')",
        )
