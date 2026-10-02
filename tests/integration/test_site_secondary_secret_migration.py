"""Additive encrypted secondary credential storage and v1.0.5 site activation."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretStore
from backend.app.application.sites import SiteService, SiteUpdate
from backend.app.config import AppSettings
from backend.app.domain.site_config import SiteCredentialKind, SiteKind, site_profile
from backend.app.infrastructure.persistence.database import sqlite_database_url
from backend.app.infrastructure.persistence.models import SecretRecord, Site
from backend.app.infrastructure.persistence.site_repositories import SiteRepository
from backend.app.infrastructure.runtime import RuntimeManager


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def test_existing_v100_site_survives_nullable_secondary_secret_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical-site.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)
    command.upgrade(config, "0029_v018_compatibility")
    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO site "
                    "(id, name, type, base_url, credential_kind, secret_id, "
                    "capabilities, connection_status, enabled, version, last_test_at, "
                    "created_at, updated_at) "
                    "VALUES ('old-site', 'existing', 'HHCLUB', 'https://hhanclub.net', "
                    "'COOKIE', NULL, '{}', 'UNTESTED', 0, 1, NULL, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, name, type, version, download_secret_id "
                    "FROM site WHERE id = 'old-site'"
                )
            ).one()
            assert tuple(row) == ("old-site", "existing", "HHCLUB", 1, None)
    finally:
        engine.dispose()


def test_v104_database_upgrades_to_cookiecloud_schema_without_touching_sites(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v104-cookiecloud-upgrade.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)
    command.upgrade(config, "0031_site_type_capacity_v101")
    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO site "
                    "(id, name, type, base_url, credential_kind, secret_id, "
                    "capabilities, connection_status, enabled, version, last_test_at, "
                    "created_at, updated_at) "
                    "VALUES ('v104-site', 'v1.0.4 KeepFrds', 'KEEPFRDS', "
                    "'https://pt.keepfrds.com', 'COOKIE', NULL, '{}', 'UNTESTED', 0, 7, NULL, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            table_names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            }
            assert "cookiecloud_setting" not in table_names

        command.upgrade(config, "0032_cookiecloud_v105")

        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == "0032_cookiecloud_v105"
            site = connection.execute(
                text(
                    "SELECT id, name, type, base_url, credential_kind, enabled, version "
                    "FROM site WHERE id = 'v104-site'"
                )
            ).one()
            assert tuple(site) == (
                "v104-site",
                "v1.0.4 KeepFrds",
                "KEEPFRDS",
                "https://pt.keepfrds.com",
                "COOKIE",
                0,
                7,
            )
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(cookiecloud_setting)"))
            }
            assert {
                "server_url",
                "uuid",
                "password_secret_id",
                "auto_sync",
                "sync_interval_minutes",
                "last_sync_status",
                "version",
            } <= columns
            assert (
                connection.execute(text("SELECT COUNT(*) FROM cookiecloud_setting")).scalar_one()
                == 0
            )
            assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    finally:
        engine.dispose()


def test_secondary_secret_storage_rotation_and_cleanup_on_existing_site(
    tmp_path: Path,
) -> None:
    """Synthetic persisted supported site exercises schema without lifting Rousi gates."""
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        service = SiteService(runtime.session_factory, store)
        main_value = b"synthetic-main-cookie"
        extra_value = b"synthetic-secondary-cookie"
        with runtime.session_factory() as session:
            main_id = store.put_in_session(session, kind="SITE_COOKIE", value=main_value)
            secondary_id = store.put_in_session(
                session, kind="SITE_DOWNLOAD_COOKIE", value=extra_value
            )
            created_site = SiteRepository(session).create(
                name="Synthetic Existing Site",
                kind=SiteKind.HHCLUB.value,
                base_url=site_profile(SiteKind.HHCLUB).base_url,
                credential_kind=SiteCredentialKind.COOKIE.value,
                secret_id=main_id,
                request_timeout_seconds=15,
                search_interval_seconds=2,
                user_agent=None,
                browser_emulation_enabled=False,
                proxy_enabled=False,
                proxy_host=None,
                proxy_port=None,
                proxy_username=None,
                proxy_secret_id=None,
                download_secret_id=secondary_id,
            )
            site_id = created_site.id
            session.commit()
        with runtime.session_factory() as session:
            site = session.get(Site, site_id)
            secret = session.get(SecretRecord, secondary_id)
            assert site is not None and site.download_secret_id == secondary_id
            assert site.secret_id == main_id
            assert secret is not None and secret.kind == "SITE_DOWNLOAD_COOKIE"
            assert extra_value not in secret.ciphertext.encode()
            assert extra_value.decode() not in str(site.__dict__)
        assert store.get(main_id) == main_value
        assert store.get(secondary_id) == extra_value
        # The public view must never reveal a secondary secret or its ID.
        view = service.get(site_id)
        assert "download_secret_id" not in str(view)
        assert extra_value.decode() not in str(view)

        # Rotating the main credential must also clear an unrelated old
        # secondary download secret to prevent accidental credential carryover.
        service.update(
            site_id,
            expected_version=1,
            update_request=SiteUpdate(
                credential_action="SET",
                credential_kind=SiteCredentialKind.COOKIE,
                credential="synthetic-new-main-cookie",
            ),
        )
        with runtime.session_factory() as session:
            rotated_site = session.get(Site, site_id)
            assert rotated_site is not None and rotated_site.version == 2
            assert rotated_site.secret_id is not None and rotated_site.secret_id != main_id
            assert rotated_site.download_secret_id is None
            assert session.get(SecretRecord, main_id) is None
            assert session.get(SecretRecord, secondary_id) is None
            new_main_id = rotated_site.secret_id
        assert store.get(new_main_id) == b"synthetic-new-main-cookie"

        # A legacy/partially staged secondary secret must also be removed on
        # site deletion, independently of the main credential.
        with runtime.session_factory() as session:
            extra_id = store.put_in_session(session, kind="SITE_DOWNLOAD_COOKIE", value=extra_value)
            site_before_delete = session.get(Site, site_id)
            assert site_before_delete is not None
            site_before_delete.download_secret_id = extra_id
            session.commit()
        service.delete(site_id, expected_version=2)
        with runtime.session_factory() as session:
            assert session.get(Site, site_id) is None
            assert session.get(SecretRecord, extra_id) is None
            assert session.get(SecretRecord, new_main_id) is None
    finally:
        runtime.stop()


def test_rousi_pro_config_is_encrypted_and_can_activate_for_tasks(tmp_path: Path) -> None:
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        service = SiteService(runtime.session_factory, store)
        created = service.create(
            name="Synthetic Rousi Candidate",
            kind=SiteKind.ROUSI_PRO,
            credential_kind=SiteCredentialKind.API_KEY,
            credential="synthetic-api-key",
            download_cookie="synthetic-download-cookie",
        )
        assert not created.enabled
        assert created.download_credential_configured
        with runtime.session_factory() as session:
            assert session.query(Site).count() == 1
            assert session.query(SecretRecord).count() == 2
        enabled = service.set_enabled(created.id, expected_version=1, enabled=True)
        assert enabled.enabled is True
        assert service.enabled_site_versions() == ((created.id, 2),)
        adapters = service.enabled_adapters()
        assert len(adapters) == 1
        assert adapters[0].config_id == created.id
        assert adapters[0].site_id == "rousi_pro"
    finally:
        runtime.stop()


def test_imported_enabled_site_with_missing_rousi_secrets_fails_closed(
    tmp_path: Path,
) -> None:
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        service = SiteService(runtime.session_factory, store)
        with runtime.session_factory() as session:
            imported = SiteRepository(session).create(
                name="Synthetic Imported Rousi",
                kind=SiteKind.ROUSI_PRO.value,
                base_url=site_profile(SiteKind.ROUSI_PRO).base_url,
                credential_kind=SiteCredentialKind.API_KEY.value,
                secret_id=None,
                request_timeout_seconds=15,
                search_interval_seconds=2,
                user_agent=None,
                browser_emulation_enabled=False,
                proxy_enabled=False,
                proxy_host=None,
                proxy_port=None,
                proxy_username=None,
                proxy_secret_id=None,
            )
            # Simulate an externally imported malformed enabled row bypassing
            # the public service validation.
            imported.enabled = True
            site_id = imported.id
            session.commit()
        with pytest.raises(ApplicationError) as blocked_adapters:
            service.enabled_adapters()
        assert blocked_adapters.value.code == "SITE_ENABLED_CONFIG_INVALID"
        assert service.enabled_site_versions() == ((site_id, 1),)
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_imported_malformed_rousi_blocks_mixed_batch_before_secret_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        service = SiteService(runtime.session_factory, store)
        with runtime.session_factory() as session:
            supported_key = store.put_in_session(
                session, kind="SITE_COOKIE", value=b"synthetic-supported-cookie"
            )
            supported = SiteRepository(session).create(
                name="A supported",
                kind=SiteKind.HHCLUB.value,
                base_url=site_profile(SiteKind.HHCLUB).base_url,
                credential_kind=SiteCredentialKind.COOKIE.value,
                secret_id=supported_key,
                request_timeout_seconds=15,
                search_interval_seconds=0,
                user_agent=None,
                browser_emulation_enabled=False,
                proxy_enabled=False,
                proxy_host=None,
                proxy_port=None,
                proxy_username=None,
                proxy_secret_id=None,
            )
            rousi_key = store.put_in_session(
                session, kind="SITE_API_KEY", value=b"synthetic-pending-api-key"
            )
            rousi = SiteRepository(session).create(
                name="Z malformed Rousi",
                kind=SiteKind.ROUSI_PRO.value,
                base_url=site_profile(SiteKind.ROUSI_PRO).base_url,
                credential_kind=SiteCredentialKind.API_KEY.value,
                secret_id=rousi_key,
                request_timeout_seconds=15,
                search_interval_seconds=0,
                user_agent=None,
                browser_emulation_enabled=False,
                proxy_enabled=False,
                proxy_host=None,
                proxy_port=None,
                proxy_username=None,
                proxy_secret_id=None,
            )
            supported.enabled = True
            rousi.enabled = True
            rousi_id = rousi.id
            session.commit()

        def reject_secret_read(_secret_id: str) -> bytes:
            raise AssertionError("invalid mixed batch must fail before any secret read")

        monkeypatch.setattr(store, "get", reject_secret_read)
        with pytest.raises(ApplicationError) as blocked_adapters:
            service.enabled_adapters()
        assert blocked_adapters.value.code == "SITE_ENABLED_CONFIG_INVALID"
        versions = dict(service.enabled_site_versions())
        assert versions[rousi_id] == 1
        assert versions[supported.id] == 1
        # Do not call the external adapter in this hostile-import/secret-gate test.
        # Removed site user-details functionality has no business-service
        # entrypoint; the remaining test/analysis paths still fail closed.
        assert not hasattr(service, "user_profile")
    finally:
        runtime.stop()
