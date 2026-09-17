import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.app.infrastructure.persistence import models as persistence_models
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import configure_sqlite_engine

_ = persistence_models
config = context.config
database_url_override = config.attributes.get("database_url")
if database_url_override is not None and not isinstance(database_url_override, str):
    raise RuntimeError("Alembic database_url override 必须是字符串")
database_url = database_url_override or os.getenv("PACKBREAKER_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


_LEGACY_COMPATIBILITY_TABLES = {
    "api_token",
    "history_scan",
    "history_scan_file",
    "history_scan_materialization",
}


def _include_object(
    object_: object, name: str | None, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    """保留历史迁移创建但已退出运行时模型的兼容表，不把它误判为待删除 schema。"""

    if not reflected or compare_to is not None:
        return True
    if type_ == "table" and name in _LEGACY_COMPATIBILITY_TABLES:
        return False
    table = getattr(object_, "table", None)
    return getattr(table, "name", None) not in _LEGACY_COMPATIBILITY_TABLES


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    configure_sqlite_engine(connectable)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
