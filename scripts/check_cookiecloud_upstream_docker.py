"""Exercise PackBreaker against the official easychen/CookieCloud HTTP server.

The target server is expected to be an isolated disposable instance. Only
synthetic UUIDs, passwords, and cookie values are written.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from collections.abc import Callable

import httpx2
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.domain.cookiecloud import CookieCloudCryptoType
from backend.app.infrastructure.cookiecloud import (
    CookieCloudClient,
    decrypt_cookiecloud_payload,
)


def _passphrase(uuid: str, password: str) -> bytes:
    return (
        hashlib.md5(  # noqa: S324 - upstream protocol compatibility
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
    encryptor = Cipher(
        algorithms.AES(_passphrase(uuid, password)),
        modes.CBC(b"\x00" * 16),
    ).encryptor()
    ciphertext = encryptor.update(_pad(json.dumps(payload).encode())) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode()


def _evp(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    derived = b""
    previous = b""
    while len(derived) < 48:
        previous = hashlib.md5(  # noqa: S324 - upstream protocol compatibility
            previous + password + salt,
            usedforsecurity=False,
        ).digest()
        derived += previous
    return derived[:32], derived[32:48]


def _legacy_encrypt(uuid: str, password: str, payload: dict[str, object]) -> str:
    salt = b"PBCC2026"
    key, iv = _evp(_passphrase(uuid, password), salt)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(_pad(json.dumps(payload).encode())) + encryptor.finalize()
    return base64.b64encode(b"Salted__" + salt + ciphertext).decode()


def _payload(case: str) -> dict[str, object]:
    return {
        "cookie_data": {
            ".example.test": [
                {
                    "name": "session",
                    "value": f"synthetic-{case}",
                    "domain": ".example.test",
                    "path": "/",
                }
            ]
        },
        "local_storage_data": {},
        "update_time": "2026-10-01T00:00:00.000Z",
    }


async def _exercise_case(
    *,
    server_url: str,
    crypto_type: CookieCloudCryptoType,
    encrypt: Callable[[str, str, dict[str, object]], str],
) -> None:
    suffix = "legacy" if crypto_type is CookieCloudCryptoType.LEGACY else "fixed"
    uuid = f"packbreaker-upstream-{suffix}"
    password = f"synthetic-password-{suffix}"
    encrypted = encrypt(uuid, password, _payload(suffix))

    async with httpx2.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        response = await client.post(
            f"{server_url}/update",
            json={
                "uuid": uuid,
                "encrypted": encrypted,
                "crypto_type": crypto_type.value,
            },
        )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("action") != "done":
        raise RuntimeError("CookieCloud /update did not report done")

    envelope = await CookieCloudClient().fetch(
        server_url=server_url,
        uuid=uuid,
        timeout_seconds=15.0,
    )
    if envelope.crypto_type is not crypto_type:
        raise RuntimeError(
            f"CookieCloud crypto detection mismatch: expected={crypto_type.value} "
            f"actual={envelope.crypto_type.value}"
        )
    payload = decrypt_cookiecloud_payload(
        uuid=uuid,
        password=password,
        encrypted=envelope.encrypted,
        crypto_type=envelope.crypto_type,
    )
    cookies = payload.cookie_data.get(".example.test", ())
    if len(cookies) != 1 or cookies[0].value != f"synthetic-{suffix}":
        raise RuntimeError("CookieCloud payload mismatch")


async def _run(server_url: str) -> None:
    base = server_url.rstrip("/")
    await _exercise_case(
        server_url=base,
        crypto_type=CookieCloudCryptoType.LEGACY,
        encrypt=_legacy_encrypt,
    )
    await _exercise_case(
        server_url=base,
        crypto_type=CookieCloudCryptoType.AES_128_CBC_FIXED,
        encrypt=_fixed_encrypt,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="验证 PackBreaker 与 easychen/CookieCloud 官方服务端兼容性"
    )
    parser.add_argument("server_url")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.server_url))
    print("CookieCloud upstream E2E passed: legacy + aes-128-cbc-fixed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
