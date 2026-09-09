from pathlib import Path

from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry


def sqlite_database_url(database_path: Path) -> str:
    return URL.create("sqlite+pysqlite", database=str(database_path)).render_as_string(
        hide_password=False
    )


def _set_sqlite_pragmas(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def configure_sqlite_engine(engine: Engine) -> Engine:
    if engine.url.get_backend_name() != "sqlite":
        return engine
    if not event.contains(engine, "connect", _set_sqlite_pragmas):
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def create_sqlite_engine(database_path: Path, *, echo: bool = False) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        sqlite_database_url(database_path),
        connect_args={"check_same_thread": False},
        echo=echo,
    )
    return configure_sqlite_engine(engine)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)
