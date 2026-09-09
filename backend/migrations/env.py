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


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
