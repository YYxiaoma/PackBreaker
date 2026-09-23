"""在候选数据库中扩展站点类型存储容量，正式启用门禁保持关闭。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031_site_type_capacity_v101"
down_revision: str | None = "0030_site_download_credential_v101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A released Alembic revision must be reproducible independently of future
# SiteKind additions. New site kinds require a *new* migration, not a changed
# historical CHECK constraint when replaying v1.0.1 from an old database.
_V101_SITE_KINDS = (
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
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("站点类型容量迁移仅支持受控的 SQLite 升级副本")

    # PRAGMA foreign_keys is a no-op inside an active SQLite transaction.
    # Exit Alembic's logical transaction before disabling it; dropping the
    # parent with it ON silently sets task_definition.site_id to NULL.
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("无法为隔离站点表迁移准备 SQLite 外键状态")

    try:
        # All existing columns, indexes, name uniqueness and site->secret FK
        # are reflected and copied by batch recreation. task_definition is
        # never rebuilt. No site records or secret references are modified.
        kinds = ", ".join(f"'{kind}'" for kind in sorted(_V101_SITE_KINDS))
        with op.batch_alter_table("site", recreate="always") as batch:
            batch.drop_constraint("type", type_="check")
            batch.create_check_constraint("type", f"type IN ({kinds})")

        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("站点类型迁移后的 SQLite 外键完整性检查失败")
        site_indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(site)")}
        if "ix_site_type_enabled" not in site_indexes:
            raise RuntimeError("站点类型迁移后缺少原有索引")
    finally:
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("站点类型迁移后未恢复 SQLite 外键检查")


def downgrade() -> None:
    # Do not rebuild the site parent or delete a candidate-site row to
    # restore the previous three-kind CHECK. Older application code still
    # refuses pending kinds at its create/enable/adapter service gates.
    pass
