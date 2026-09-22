"""Real SQLite upgrade copy must preserve existing site identities and task links."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from backend.app.domain.site_config import DATABASE_SITE_KINDS, PERSISTED_SITE_KINDS
from backend.app.infrastructure import runtime as runtime_module
from backend.app.infrastructure.backups import BackupError
from backend.app.infrastructure.persistence.database import sqlite_database_url
from backend.app.infrastructure.runtime import InstanceLock
from backend.app.infrastructure.upgrades import upgrade_database_safely


def _historic_linked_database(path: Path) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(path)
    command.upgrade(config, "0029_v018_compatibility")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO site "
            "(id, name, type, base_url, credential_kind, secret_id, "
            "capabilities, connection_status, enabled, version, last_test_at, "
            "created_at, updated_at) "
            "VALUES ('old-site', 'synthetic', 'HHCLUB', 'https://hhanclub.net', "
            "'COOKIE', NULL, '{}', 'UNTESTED', 0, 1, NULL, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO task_definition "
            "(id, name, kind, status, site_id, version, created_at, updated_at) "
            "VALUES ('old-task', 'synthetic', 'MANUAL', 'PAUSED', 'old-site', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )


def _identity(path: Path) -> tuple[tuple[str, str, int], tuple[str, str | None]]:
    with sqlite3.connect(path) as connection:
        site = connection.execute(
            "SELECT id, type, version FROM site WHERE id='old-site'"
        ).fetchone()
        task = connection.execute(
            "SELECT id, site_id FROM task_definition WHERE id='old-task'"
        ).fetchone()
        assert site is not None and task is not None
        return site, task


def _upgrade(path: Path, *, backup_dir: Path) -> None:
    lock = InstanceLock(path.with_suffix(".lock"))
    lock.acquire()
    try:
        upgrade_database_safely(
            path,
            instance_lock=lock,
            safety_backup_dir=backup_dir,
            app_version="1.0.1-synthetic-test",
        )
    finally:
        lock.release()


def test_sqlite_parent_table_rebuild_can_silently_null_task_site_reference() -> None:
    """Prove foreign_key_check cannot defend against a naive parent rebuild."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            "CREATE TABLE site(id TEXT PRIMARY KEY, type TEXT NOT NULL "
            "CHECK(type IN ('HHCLUB')));"
            "CREATE TABLE task_definition(id TEXT PRIMARY KEY, "
            "site_id TEXT REFERENCES site(id) ON DELETE SET NULL);"
            "INSERT INTO site VALUES('s1','HHCLUB');"
            "INSERT INTO task_definition VALUES('t1','s1');"
        )
        connection.execute("PRAGMA defer_foreign_keys=ON")
        connection.execute(
            "CREATE TABLE site_new(id TEXT PRIMARY KEY, type TEXT NOT NULL "
            "CHECK(type IN ('HHCLUB','ROUSI_PRO')))"
        )
        connection.execute("INSERT INTO site_new SELECT * FROM site")
        connection.execute("DROP TABLE site")
        connection.execute("ALTER TABLE site_new RENAME TO site")
        assert connection.execute("SELECT site_id FROM task_definition").fetchone() == (None,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_site_relation_guard_preserves_real_historic_rows_during_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historic.db"
    _historic_linked_database(database)
    expected = _identity(database)
    _upgrade(database, backup_dir=tmp_path / "backups")
    assert _identity(database) == expected
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT download_secret_id FROM site").fetchone() == (None,)


def test_expanded_site_constraint_keeps_indexes_and_all_original_foreign_keys(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical-linked-site.db"
    _historic_linked_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO secret (id, kind, ciphertext, key_version, created_at, updated_at) "
            "VALUES ('synthetic-secret', 'SITE_COOKIE', 'synthetic-encrypted-value', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute("UPDATE site SET secret_id='synthetic-secret' WHERE id='old-site'")
    _upgrade(database, backup_dir=tmp_path / "backups")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT secret_id FROM site WHERE id='old-site'").fetchone() == (
            "synthetic-secret",
        )
        assert connection.execute(
            "SELECT ciphertext FROM secret WHERE id='synthetic-secret'"
        ).fetchone() == ("synthetic-encrypted-value",)
        assert connection.execute(
            "SELECT site_id FROM task_definition WHERE id='old-task'"
        ).fetchone() == ("old-site",)
        site_foreign_keys = connection.execute("PRAGMA foreign_key_list(site)").fetchall()
        assert any(
            key[2] == "secret" and key[3] == "secret_id" and key[6] == "SET NULL"
            for key in site_foreign_keys
        )
        task_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(task_definition)"
        ).fetchall()
        assert any(
            key[2] == "site" and key[3] == "site_id" and key[6] == "SET NULL"
            for key in task_foreign_keys
        )
        indexes = {entry[1] for entry in connection.execute("PRAGMA index_list(site)")}
        assert "ix_site_type_enabled" in indexes
        site_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='site'"
        ).fetchone()
        assert site_sql is not None and isinstance(site_sql[0], str)
        type_check = re.search(r"\btype\s+IN\s*\(([^)]*)\)", site_sql[0])
        assert type_check is not None
        # Snapshot the exact v1.0.1 revision contract: changing the runtime
        # enum in a later version must not silently rewrite historical DDL.
        declared_types = re.findall(r"'([^']+)'", type_check.group(1))
        assert len(declared_types) == 11
        assert set(declared_types) == {
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
        }
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO site (id, name, type, base_url, credential_kind, "
                "capabilities, connection_status, enabled, version, created_at, updated_at) "
                "VALUES ('duplicate-name', 'synthetic', 'ROUSI_PRO', "
                "'https://candidate.invalid', 'COOKIE', '{}', "
                "'UNTESTED', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        # The schema accepts new types without modifying the service's
        # separate production allowlist; every inserted candidate is disabled.
        for kind in sorted(DATABASE_SITE_KINDS - PERSISTED_SITE_KINDS):
            connection.execute(
                "INSERT INTO site (id, name, type, base_url, credential_kind, "
                "capabilities, connection_status, enabled, version, created_at, updated_at) "
                "VALUES (?, ?, ?, 'https://candidate.invalid', 'COOKIE', '{}', "
                "'UNTESTED', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (f"candidate-{kind.value}", f"candidate-{kind.value}", kind.value),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO site (id, name, type, base_url, credential_kind, "
                "capabilities, connection_status, enabled, version, created_at, updated_at) "
                "VALUES ('invalid-kind', 'invalid-kind', 'UNKNOWN_KIND', "
                "'https://candidate.invalid', 'COOKIE', '{}', "
                "'UNTESTED', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM site WHERE type != 'HHCLUB' AND enabled=0"
        ).fetchone() == (len(DATABASE_SITE_KINDS - PERSISTED_SITE_KINDS),)
        assert connection.execute(
            "SELECT site_id FROM task_definition WHERE id='old-task'"
        ).fetchone() == ("old-site",)


@pytest.mark.parametrize("sabotage", ("unlink_task", "change_site_type"))
def test_site_relation_guard_rejects_fk_valid_but_destructive_upgrade_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sabotage: str,
) -> None:
    database = tmp_path / "historic.db"
    _historic_linked_database(database)
    expected = _identity(database)
    original_migrate = runtime_module.migrate_database

    def simulated_bad_migration(database_url: str) -> None:
        original_migrate(database_url)
        from sqlalchemy.engine import make_url

        candidate = make_url(database_url).database
        assert candidate is not None
        with closing(sqlite3.connect(candidate)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                if sabotage == "unlink_task":
                    connection.execute(
                        "UPDATE task_definition SET site_id=NULL WHERE id='old-task'"
                    )
                else:
                    connection.execute("UPDATE site SET type='HDTIME' WHERE id='old-site'")
                # The deliberately bad change is FK-valid: ordinary integrity
                # checks would not detect it, so the relationship guard must.
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    monkeypatch.setattr(runtime_module, "migrate_database", simulated_bad_migration)
    with pytest.raises(BackupError, match="当前数据库未切换"):
        _upgrade(database, backup_dir=tmp_path / "backups")
    assert _identity(database) == expected
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0029_v018_compatibility",
        )


def test_site_relation_guard_preserves_existing_download_secret_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An FK-valid, silently cleared v1.0.1 Cookie reference must block cutover."""
    database = tmp_path / "secondary-cookie-existing.db"
    _historic_linked_database(database)
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)
    command.upgrade(config, "0030_site_download_credential_v101")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO secret (id, kind, ciphertext, key_version, created_at, updated_at) "
            "VALUES ('synthetic-secondary', 'SITE_DOWNLOAD_COOKIE', 'synthetic-encrypted', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "UPDATE site SET download_secret_id='synthetic-secondary' WHERE id='old-site'"
        )

    healthy = tmp_path / "secondary-cookie-healthy.db"
    # A plain file copy can miss an active SQLite WAL. Use SQLite's backup API
    # so the healthy comparison includes the just-committed secret reference.
    with (
        closing(sqlite3.connect(database)) as original,
        closing(sqlite3.connect(healthy)) as target,
    ):
        original.backup(target)
    _upgrade(healthy, backup_dir=tmp_path / "healthy-backups")
    with sqlite3.connect(healthy) as connection:
        assert connection.execute(
            "SELECT download_secret_id FROM site WHERE id='old-site'"
        ).fetchone() == ("synthetic-secondary",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    original_migrate = runtime_module.migrate_database

    def simulated_bad_migration(database_url: str) -> None:
        original_migrate(database_url)
        from sqlalchemy.engine import make_url

        candidate = make_url(database_url).database
        assert candidate is not None
        with closing(sqlite3.connect(candidate)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("UPDATE site SET download_secret_id=NULL WHERE id='old-site'")
            connection.commit()
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    monkeypatch.setattr(runtime_module, "migrate_database", simulated_bad_migration)
    with pytest.raises(BackupError, match="当前数据库未切换"):
        _upgrade(database, backup_dir=tmp_path / "backups")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT download_secret_id FROM site WHERE id='old-site'"
        ).fetchone() == ("synthetic-secondary",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0030_site_download_credential_v101",
        )
