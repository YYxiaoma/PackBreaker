import os
import stat
from pathlib import Path

import pytest

from backend.app.infrastructure.security import (
    MasterKeyFile,
    PasswordService,
    SecretCipher,
    SecretDecryptionError,
    SecretKeyError,
)


def test_master_key_is_created_once_with_restricted_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "config" / "secret.key"

    first = MasterKeyFile.load_or_create(key_path)
    second = MasterKeyFile.load_or_create(key_path)

    assert first == second
    assert len(first) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_master_key_rejects_group_or_world_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "secret.key"
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o644)

    with pytest.raises(SecretKeyError):
        MasterKeyFile.load_or_create(key_path)


def test_secret_cipher_binds_ciphertext_to_identity_and_kind() -> None:
    cipher = SecretCipher(os.urandom(32))
    plaintext = b"secret-canary-value"
    encrypted = cipher.encrypt(
        secret_id="secret-a",
        kind="DOWNLOADER_PASSWORD",
        key_version=1,
        plaintext=plaintext,
    )

    assert plaintext not in encrypted.encode()
    assert (
        cipher.decrypt(
            secret_id="secret-a",
            kind="DOWNLOADER_PASSWORD",
            key_version=1,
            ciphertext=encrypted,
        )
        == plaintext
    )
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(
            secret_id="secret-b",
            kind="DOWNLOADER_PASSWORD",
            key_version=1,
            ciphertext=encrypted,
        )


def test_password_service_uses_one_way_argon2_hash() -> None:
    service = PasswordService()
    password = "a sufficiently long synthetic password"

    password_hash = service.hash(password)
    verified, _ = service.verify(password_hash, password)
    rejected, _ = service.verify(password_hash, "different synthetic password")

    assert password not in password_hash
    assert password_hash.startswith("$argon2id$")
    assert verified
    assert not rejected
