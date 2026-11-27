"""统一 seeding_watch 检查约束的命名前缀。

0057 建表、0059 重建表时传的都是已经带 ``ck_seeding_watch_`` 前缀的完整名字，
MetaData 的命名约定 ``ck_%(table_name)s_%(constraint_name)s`` 会再套一层，库里的
真实名字因此变成 ``ck_seeding_watch_ck_seeding_watch_presence``。

后果有两层：按模型里的短名删约束找不到对象，按 ``op.f("ck_seeding_watch_...")``
删同样找不到——batch 模式会把反射到的名字再套一次约定。名字统一回单前缀后，
``ck_seeding_watch_presence`` 就是模型、迁移、下游脚本共用的唯一标识。

SQLite 不能改约束名，只能重建表。重建时必须同时做两件事才能按字面名字操作：
``naming_convention={}`` 让 batch 反射时不套前缀，``op.f(...)`` 让名字转换不套前缀。
少任何一个，drop 都会因为名字被多套一层而找不到对象。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0067_seeding_watch_constraint_names"
down_revision: str | None = "0066_coverage_scope_and_tracker_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREFIX = "ck_seeding_watch_"
_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "identity_source",
        "identity_source IS NULL OR identity_source IN "
        "('COMMENT', 'TRACKER', 'PLAN', 'NAME_SIZE', 'MANUAL')",
    ),
    ("presence", "presence IN ('ACTIVE', 'GONE', 'SITE_ONLY')"),
    ("size_bytes", "size_bytes >= 0"),
    ("seeding_time_seconds", "seeding_time_seconds IS NULL OR seeding_time_seconds >= 0"),
    ("site_outcome", "site_outcome IS NULL OR site_outcome IN ('FOUND', 'MISSING', 'UNKNOWN')"),
    ("consecutive_misses", "consecutive_misses >= 0"),
)


def _check_names(connection: Connection) -> set[str]:
    names: set[str] = set()
    for item in sa.inspect(connection).get_check_constraints("seeding_watch"):
        name = item.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _rename(connection: Connection, *, to_legacy: bool) -> None:
    existing = _check_names(connection)
    plans: list[tuple[str, str, str]] = []
    for token, expression in _CHECKS:
        single = f"{_PREFIX}{token}"
        legacy = f"{_PREFIX}{single}"
        source, target = (single, legacy) if to_legacy else (legacy, single)
        # 已经是对的、或两种名字同时存在（无从判断该删哪个）都跳过，保持幂等。
        if source in existing and target not in existing:
            plans.append((source, target, expression))
    if not plans:
        return
    with op.batch_alter_table("seeding_watch", recreate="always", naming_convention={}) as batch:
        for source, target, expression in plans:
            batch.drop_constraint(op.f(source), type_="check")
            batch.create_check_constraint(op.f(target), expression)


def upgrade() -> None:
    _rename(op.get_bind(), to_legacy=False)


def downgrade() -> None:
    # 回退到 0066 时代的历史形态，让 0066 的 downgrade 看到它预期的名字。
    _rename(op.get_bind(), to_legacy=True)
