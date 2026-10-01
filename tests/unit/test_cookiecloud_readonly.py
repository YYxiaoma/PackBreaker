from __future__ import annotations

import base64
import hashlib
import json

import httpx2
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.infrastructure.cookiecloud import CookieCloudClient
from scripts.check_cookiecloud_readonly import check_cookiecloud


def _encrypt(uuid: str, password: str, payload: dict[str, object]) -> str:
    key = (
        hashlib.md5(  # noqa: S324 - protocol fixture
            f"{uuid}-{password}".encode(),
            usedforsecurity=False,
        )
        .hexdigest()[:16]
        .encode()
    )
    padder = padding.PKCS7(128).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


@pytest.mark.asyncio
async def test_live_cookiecloud_acceptance_is_read_only_and_redacts_secrets() -> None:
    uuid = "synthetic-live-uuid"
    password = "synthetic-live-password"
    encrypted = _encrypt(
        uuid,
        password,
        {
            "cookie_data": {
                ".example.test": [
                    {
                        "name": "session",
                        "value": "private-cookie-value",
                        "domain": ".example.test",
                        "path": "/",
                    }
                ]
            },
            "local_storage_data": {},
            "update_time": "2026-10-01T07:00:00.000Z",
        },
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://cookie.example.test/get/synthetic-live-uuid"
        assert password not in str(request.url)
        assert request.content == b""
        return httpx2.Response(
            200,
            json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        )

    result = await check_cookiecloud(
        {
            "server_url": "https://cookie.example.test",
            "uuid": uuid,
            "password": password,
        },
        client=CookieCloudClient(transport=httpx2.MockTransport(handler)),
    )

    assert result == {
        "status": "VALID_PAYLOAD",
        "crypto_type": "aes-128-cbc-fixed",
        "domain_count": 1,
        "cookie_count": 1,
        "update_time_present": True,
        "site_state_mutated": False,
        "password_sent_to_server": False,
    }
    serialized = json.dumps(result)
    assert password not in serialized
    assert "private-cookie-value" not in serialized


@pytest.mark.asyncio
async def test_live_cookiecloud_acceptance_rejects_invalid_config_without_network() -> None:
    called = False

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal called
        called = True
        return httpx2.Response(500)

    result = await check_cookiecloud(
        {"server_url": "https://cookie.example.test", "uuid": "", "password": "x"},
        client=CookieCloudClient(transport=httpx2.MockTransport(handler)),
    )

    assert result == {"status": "CONFIG_BLOCKED"}
    assert called is False
