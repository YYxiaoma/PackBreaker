"""持久化辅种覆盖率扫描范围，并允许以 tracker 作为种子身份来源。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0066_coverage_scope_and_tracker_identity"
down_revision: str | None = "0065_media_coverage_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_SOURCES = "'COMMENT', 'TRACKER', 'PLAN', 'NAME_SIZE', 'MANUAL'"
_OLD_SOURCES = "'COMMENT', 'PLAN', 'NAME_SIZE', 'MANUAL'"

# 0057 建表时传的是已带表前缀的完整约束名，命名约定又套了一层，旧库里的真实名字是
# 双重前缀（0067 之后统一回单前缀）。两种都要认，迁移才能在新库、旧库、测试库上都成立。
_LEGACY_NAME = "ck_seeding_watch_ck_seeding_watch_identity_source"
_PREFERRED_NAME = "ck_seeding_watch_identity_source"


def _check_names(connection: Connection) -> set[str]:
    names: set[str] = set()
    for item in sa.inspect(connection).get_check_constraints("seeding_watch"):
        name = item.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _identity_source_constraint(connection: Connection) -> str:
    names = _check_names(connection)
    for candidate in (_LEGACY_NAME, _PREFERRED_NAME):
        if candidate in names:
            return candidate
    raise RuntimeError("seeding_watch 缺少 identity_source 检查约束")


def upgrade() -> None:
    op.create_table(
        "media_coverage_scope",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("selection_token", sa.Text(), nullable=False),
        sa.Column("scope_label", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    name = _identity_source_constraint(op.get_bind())
    # 显式传空命名约定 + op.f()：默认命名约定会把传进来的名字再套一层表前缀
    # （batch 反射和名字转换各套一次），结果谁也匹配不上。
    with op.batch_alter_table("seeding_watch", naming_convention={}) as batch:
        batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(
            op.f(_PREFERRED_NAME),
            f"identity_source IS NULL OR identity_source IN ({_NEW_SOURCES})",
        )


def downgrade() -> None:
    # 旧约束不允许 TRACKER。库里已有这批数据时必须先降级语义再换约束，
    # 否则重建表那一步会直接违反 CHECK。置空表示「来源未知」，下一轮巡查会重算。
    op.execute("UPDATE seeding_watch SET identity_source = NULL WHERE identity_source = 'TRACKER'")
    name = _identity_source_constraint(op.get_bind())
    with op.batch_alter_table("seeding_watch", naming_convention={}) as batch:
        batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(
            op.f(_PREFERRED_NAME),
            f"identity_source IS NULL OR identity_source IN ({_OLD_SOURCES})",
        )
    op.drop_table("media_coverage_scope")
