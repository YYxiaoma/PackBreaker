"""扩展 v1.1.1 七个待验证站点的数据库类型容量，不改变启用门禁。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045_seven_site_capacity_v111"
down_revision: str | None = "0044_retire_legacy_task_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V111_SITE_KINDS = (
    "MTEAM",
    "HDTIME",
    "HHCLUB",
    "KEEPFRDS",
    "HDHOME",
    "UBITS",
    "HDFANS",
    "BTSCHOOL",
    "PTTIME",
    "ROUSI_PRO",
    "LINGYIN_CLUB",
    "PTERCLUB",
    "AUDIENCES",
    "SPRING_SUNDAY",
    "HDDOLBY",
    "U2",
    "TANGPT",
    "CARPT",
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("站点类型容量迁移仅支持 SQLite")
    # The old parent table may be referenced by task_definition.site_id.
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("无法准备站点表迁移的 SQLite 外键状态")
    try:
        kinds = ", ".join(f"'{kind}'" for kind in sorted(_V111_SITE_KINDS))
        with op.batch_alter_table("site", recreate="always") as batch:
            batch.drop_constraint("type", type_="check")
            batch.create_check_constraint("type", f"type IN ({kinds})")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("站点类型扩容后存在外键完整性错误")
        indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(site)")}
        if "ix_site_type_enabled" not in indexes:
            raise RuntimeError("站点类型扩容后缺少站点索引")
    finally:
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("站点类型扩容后无法恢复外键检查")


def downgrade() -> None:
    # Do not destructively delete site rows, task links or secret references.
    pass
