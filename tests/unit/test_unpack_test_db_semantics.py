"""Unpack tests must use the same SQLite FK semantics as production."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from backend.app.infrastructure.persistence.database import configure_sqlite_engine


def test_unpack_test_engine_enables_foreign_keys() -> None:
    engine = configure_sqlite_engine(create_engine("sqlite+pysqlite:///:memory:"))
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
