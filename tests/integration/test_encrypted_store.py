from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.application.secrets import SecretStore
from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.models import SecretRecord
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.security import SecretDecryptionError


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def test_secret_store_keeps_canary_out_of_plain_database_fields(tmp_path: Path) -> None:
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        canary = b"PACKBREAKER-SECRET-CANARY-7b58"
        secret_id = store.put(kind="DOWNLOADER_PASSWORD", value=canary)

        with runtime.session_factory() as session:
            record = session.scalar(select(SecretRecord).where(SecretRecord.id == secret_id))
            assert record is not None
            assert canary not in record.ciphertext.encode()
            assert record.kind == "DOWNLOADER_PASSWORD"
            assert record.key_version == 1

        assert store.get(secret_id) == canary
    finally:
        runtime.stop()


def test_secret_cipher_detects_database_context_tampering(tmp_path: Path) -> None:
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        secret_id = store.put(kind="SITE_PASSKEY", value=b"synthetic-passkey")
        with runtime.session_factory() as session:
            record = session.get(SecretRecord, secret_id)
            assert record is not None
            record.kind = "DOWNLOADER_PASSWORD"
            session.commit()

        with pytest.raises(SecretDecryptionError):
            store.get(secret_id)
    finally:
        runtime.stop()
