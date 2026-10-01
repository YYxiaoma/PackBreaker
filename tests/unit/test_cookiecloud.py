from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.domain.cookiecloud import (
    CookieCloudCryptoType,
    cookie_domain_matches_host,
    cookie_header_for_host,
    normalize_cookiecloud_server_url,
)
from backend.app.infrastructure.cookiecloud import (
    CookieCloudClient,
    CookieCloudError,
    decrypt_cookiecloud_payload,
)


def _passphrase(uuid: str, password: str) -> bytes:
    return (
        hashlib.md5(  # noqa: S324 - protocol fixture
            f"{uuid}-{password}".encode(),
            usedforsecurity=False,
        )
        .hexdigest()[:16]
        .encode()
    )


def _pad(value: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    return padder.update(value) + padder.finalize()


def _fixed_encrypt(uuid: str, password: str, payload: dict[str, object]) -> str:
    key = _passphrase(uuid, password)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    ciphertext = encryptor.update(_pad(json.dumps(payload).encode())) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode()


def _evp(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    result = b""
    previous = b""
    while len(result) < 48:
        previous = hashlib.md5(  # noqa: S324 - protocol fixture
            previous + password + salt,
            usedforsecurity=False,
        ).digest()
        result += previous
    return result[:32], result[32:48]


def _legacy_encrypt(uuid: str, password: str, payload: dict[str, object]) -> str:
    salt = b"12345678"
    key, iv = _evp(_passphrase(uuid, password), salt)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(_pad(json.dumps(payload).encode())) + encryptor.finalize()
    return base64.b64encode(b"Salted__" + salt + ciphertext).decode()


def _payload() -> dict[str, object]:
    return {
        "cookie_data": {
            ".example.com": [
                {
                    "name": "session",
                    "value": "synthetic",
                    "domain": ".example.com",
                    "path": "/",
                }
            ]
        },
        "local_storage_data": {"https://example.com": {"theme": "dark"}},
        "update_time": "2026-09-30T12:00:00.000Z",
    }


@pytest.mark.parametrize(
    ("crypto_type", "encrypt"),
    (
        (CookieCloudCryptoType.LEGACY, _legacy_encrypt),
        (CookieCloudCryptoType.AES_128_CBC_FIXED, _fixed_encrypt),
    ),
)
def test_decrypts_easychen_cookiecloud_formats(
    crypto_type: CookieCloudCryptoType,
    encrypt: Callable[[str, str, dict[str, object]], str],
) -> None:
    uuid = "synthetic-uuid"
    password = "synthetic-password"
    encrypted = encrypt(uuid, password, _payload())
    result = decrypt_cookiecloud_payload(
        uuid=uuid,
        password=password,
        encrypted=encrypted,
        crypto_type=crypto_type,
    )
    assert result.update_time == "2026-09-30T12:00:00.000Z"
    assert result.cookie_data[".example.com"][0].name == "session"
    assert result.cookie_data[".example.com"][0].value == "synthetic"
    assert result.local_storage_data["https://example.com"] == {"theme": "dark"}


def test_wrong_cookiecloud_password_fails_without_plaintext() -> None:
    encrypted = _fixed_encrypt("uuid", "right-password", _payload())
    with pytest.raises(CookieCloudError) as exc:
        decrypt_cookiecloud_payload(
            uuid="uuid",
            password="wrong-password",
            encrypted=encrypted,
            crypto_type=CookieCloudCryptoType.AES_128_CBC_FIXED,
        )
    assert exc.value.code == "COOKIECLOUD_DECRYPT_FAILED"
    assert "right-password" not in str(exc.value)
    assert "wrong-password" not in str(exc.value)


def test_cookie_domain_matching_obeys_domain_boundary() -> None:
    assert cookie_domain_matches_host(".example.com", "pt.example.com")
    assert cookie_domain_matches_host("pt.example.com", "pt.example.com")
    assert not cookie_domain_matches_host("example.com", "notexample.com")
    assert not cookie_domain_matches_host("pt.example.com", "example.com")


def test_cookie_header_filters_expired_and_unrelated_domains() -> None:
    raw = _payload()
    raw["cookie_data"] = {
        ".example.com": [
            {"name": "session", "value": "ok", "domain": ".example.com", "path": "/"},
            {
                "name": "expired",
                "value": "old",
                "domain": ".example.com",
                "path": "/",
                "expirationDate": datetime.now(UTC).timestamp() - 60,
            },
        ],
        ".invalid.test": [
            {"name": "foreign", "value": "no", "domain": ".invalid.test", "path": "/"}
        ],
    }
    encrypted = _fixed_encrypt("uuid", "password", raw)
    payload = decrypt_cookiecloud_payload(
        uuid="uuid",
        password="password",
        encrypted=encrypted,
        crypto_type=CookieCloudCryptoType.AES_128_CBC_FIXED,
    )
    assert cookie_header_for_host(payload, "pt.example.com") == "session=ok"


def test_server_url_preserves_easychen_api_root() -> None:
    assert normalize_cookiecloud_server_url("https://cookie.example.test/api/") == (
        "https://cookie.example.test/api"
    )
    with pytest.raises(ValueError):
        normalize_cookiecloud_server_url("https://user:pass@cookie.example.test")


@pytest.mark.asyncio
async def test_client_fetches_get_uuid_without_sending_password() -> None:
    encrypted = _fixed_encrypt("uuid with space", "password", _payload())

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://cookie.example.test/root/get/uuid%20with%20space"
        assert "password" not in str(request.url)
        return httpx2.Response(
            200,
            json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        )

    result = await CookieCloudClient(transport=httpx2.MockTransport(handler)).fetch(
        server_url="https://cookie.example.test/root",
        uuid="uuid with space",
        timeout_seconds=10,
    )
    assert result.crypto_type is CookieCloudCryptoType.AES_128_CBC_FIXED
    assert result.encrypted == encrypted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected", "encrypted"),
    (
        (
            CookieCloudCryptoType.LEGACY,
            _legacy_encrypt("uuid", "password", _payload()),
        ),
        (
            CookieCloudCryptoType.AES_128_CBC_FIXED,
            _fixed_encrypt("uuid", "password", _payload()),
        ),
    ),
)
async def test_client_infers_crypto_when_server_omits_crypto_type(
    expected: CookieCloudCryptoType,
    encrypted: str,
) -> None:
    client = CookieCloudClient(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, json={"encrypted": encrypted})
        )
    )
    result = await client.fetch(
        server_url="https://cookie.example.test",
        uuid="uuid",
        timeout_seconds=5,
    )
    assert result.crypto_type is expected


@pytest.mark.asyncio
async def test_client_rejects_redirects_and_unknown_crypto() -> None:
    redirects = CookieCloudClient(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(302, headers={"location": "https://outside.invalid"})
        )
    )
    with pytest.raises(CookieCloudError) as exc:
        await redirects.fetch(server_url="https://cookie.example.test", uuid="u", timeout_seconds=5)
    assert exc.value.code == "COOKIECLOUD_REDIRECT_REJECTED"

    unknown = CookieCloudClient(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(
                200,
                json={"encrypted": "abc", "crypto_type": "future"},
            )
        )
    )
    with pytest.raises(CookieCloudError) as exc:
        await unknown.fetch(server_url="https://cookie.example.test", uuid="u", timeout_seconds=5)
    assert exc.value.code == "COOKIECLOUD_CRYPTO_UNSUPPORTED"
