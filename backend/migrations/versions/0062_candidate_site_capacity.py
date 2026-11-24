"""扩展 site.type 的 CHECK 约束，容纳来自上游站点定义的候选站点类型。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0062_candidate_site_capacity"
down_revision: str | None = "0061_notification_product_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CANDIDATE_SITE_KINDS = (
    "13CITY",
    "1PTBA",
    "52MOVIE",
    "52PT",
    "AGSVPT",
    "ALINGPT",
    "AUDIENCES",
    "BAOZI",
    "BEITAI",
    "BTSCHOOL",
    "BYRBT",
    "CARPT",
    "CBG",
    "CDY",
    "CHDBITS",
    "CNSCG",
    "CSPT",
    "CYANBUG",
    "DABAOJIAN",
    "DAJIAO",
    "DHTCLUB",
    "DISCFAN",
    "DRAGONHD",
    "DSTUDIO",
    "DUBHE",
    "ECUSTPT",
    "FHPT",
    "FILEPT",
    "FREEFARM",
    "GAINBOUND",
    "HAIDAN",
    "HARES",
    "HD4FANS",
    "HDAI",
    "HDAREA",
    "HDATMOS",
    "HDCITY",
    "HDDISK",
    "HDDOLBY",
    "HDFANS",
    "HDHOME",
    "HDKYLIN",
    "HDPT",
    "HDSKY",
    "HDSTREET",
    "HDTIME",
    "HDUPT",
    "HDVIDEO",
    "HDZONE",
    "HHCLUB",
    "HITPT",
    "HSPT",
    "HUDBT",
    "ICC2022",
    "ITZMX",
    "JOYHD",
    "KEEPFRDS",
    "KUFEI",
    "KUNLUN",
    "LAJIDUI",
    "LEMONHD",
    "LEMONHDNET",
    "LEMONHDORG",
    "LINGYIN_CLUB",
    "MARCH",
    "MOECAT",
    "MTEAM",
    "MUXUEGE",
    "MYPT",
    "NANYANGPT",
    "NOVAHD",
    "NPUPT",
    "NWSUAF6",
    "OURBITS",
    "PT99",
    "PTCHINA",
    "PTDREAM",
    "PTERCLUB",
    "PTGTK",
    "PTHOME",
    "PTLGS",
    "PTMSG",
    "PTSBAO",
    "PTTIME",
    "PTVICOMO",
    "PTZONE",
    "QINGYINGPT",
    "RAILGUNPT",
    "RAINGFH",
    "RETROFLIX",
    "ROUSI_PRO",
    "RS",
    "SBPT",
    "SEWERPT",
    "SHARKPT",
    "SJTU",
    "SPRING_SUNDAY",
    "STSPT",
    "TANGPT",
    "TCCF",
    "TEY",
    "TJUPT",
    "TLFBITS",
    "TMPT",
    "TOKYOPT",
    "TOSKY",
    "U2",
    "UBITS",
    "ULTRAHD",
    "WHUPT",
    "WINTERSAKURA",
    "XDYPT",
    "XINGWAN",
    "XINGYUNGE",
    "YINGK",
    "YINGUSKG",
    "ZRPT",
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("站点类型容量迁移仅支持 SQLite")
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("无法准备站点表迁移的 SQLite 外键状态")
    try:
        kinds = ", ".join(f"'{kind}'" for kind in sorted(_CANDIDATE_SITE_KINDS))
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
    # Do not rebuild the site parent or delete candidate-site rows to restore the
    # previous CHECK: a routine downgrade must not destroy user data. Older
    # application code still refuses unsupported kinds at its own service gates,
    # so keeping the widened constraint is the safe and reversible outcome.
    pass
