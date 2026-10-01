from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx2
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.domain.cookiecloud import (
    CookieCloudCookie,
    CookieCloudCryptoType,
    CookieCloudPayload,
    normalize_cookiecloud_server_url,
    normalize_cookiecloud_uuid,
)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CookieCloudError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CookieCloudEnvelope:
    encrypted: str
    crypto_type: CookieCloudCryptoType


class CookieCloudClient:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(
        self,
        *,
        server_url: str,
        uuid: str,
        timeout_seconds: float,
    ) -> CookieCloudEnvelope:
        base_url = normalize_cookiecloud_server_url(server_url)
        normalized_uuid = normalize_cookiecloud_uuid(uuid)
        if timeout_seconds <= 0:
            raise ValueError("CookieCloud timeout 必须为正数")
        endpoint = f"{base_url}/get/{quote(normalized_uuid, safe='')}"
        try:
            async with httpx2.AsyncClient(
                timeout=timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(endpoint, headers={"Accept": "application/json"})
        except httpx2.TimeoutException as exc:
            raise CookieCloudError("COOKIECLOUD_TIMEOUT", retryable=True) from exc
        except httpx2.RequestError as exc:
            raise CookieCloudError("COOKIECLOUD_CONNECTION_FAILED", retryable=True) from exc
        if 300 <= response.status_code < 400:
            raise CookieCloudError("COOKIECLOUD_REDIRECT_REJECTED", retryable=False)
        if response.status_code == 404:
            raise CookieCloudError("COOKIECLOUD_UUID_NOT_FOUND", retryable=False)
        if response.status_code >= 500:
            raise CookieCloudError("COOKIECLOUD_SERVER_ERROR", retryable=True)
        if not 200 <= response.status_code < 300:
            raise CookieCloudError("COOKIECLOUD_REQUEST_FAILED", retryable=False)
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise CookieCloudError("COOKIECLOUD_RESPONSE_TOO_LARGE", retryable=False)
        try:
            value = response.json()
        except ValueError as exc:
            raise CookieCloudError("COOKIECLOUD_INVALID_RESPONSE", retryable=False) from exc
        if not isinstance(value, dict):
            raise CookieCloudError("COOKIECLOUD_INVALID_RESPONSE", retryable=False)
        encrypted = value.get("encrypted")
        if not isinstance(encrypted, str) or not encrypted:
            raise CookieCloudError("COOKIECLOUD_INVALID_RESPONSE", retryable=False)
        raw_crypto_type = value.get("crypto_type")
        if raw_crypto_type is None:
            crypto_type = _infer_cookiecloud_crypto_type(encrypted)
        else:
            try:
                crypto_type = CookieCloudCryptoType(raw_crypto_type)
            except (TypeError, ValueError) as exc:
                raise CookieCloudError("COOKIECLOUD_CRYPTO_UNSUPPORTED", retryable=False) from exc
        return CookieCloudEnvelope(encrypted=encrypted, crypto_type=crypto_type)


def decrypt_cookiecloud_payload(
    *,
    uuid: str,
    password: str,
    encrypted: str,
    crypto_type: CookieCloudCryptoType,
) -> CookieCloudPayload:
    normalized_uuid = normalize_cookiecloud_uuid(uuid)
    if not password or len(password) > 512:
        raise CookieCloudError("COOKIECLOUD_PASSWORD_INVALID", retryable=False)
    passphrase = (
        hashlib.md5(  # noqa: S324 - protocol compatibility with easychen/CookieCloud
            f"{normalized_uuid}-{password}".encode(),
            usedforsecurity=False,
        )
        .hexdigest()[:16]
        .encode("ascii")
    )
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CookieCloudError("COOKIECLOUD_DECRYPT_FAILED", retryable=False) from exc

    try:
        if crypto_type is CookieCloudCryptoType.AES_128_CBC_FIXED:
            plaintext = _decrypt_aes_cbc(
                ciphertext=raw,
                key=passphrase,
                iv=b"\x00" * 16,
            )
        else:
            if len(raw) < 32 or raw[:8] != b"Salted__":
                raise ValueError("legacy ciphertext header missing")
            salt = raw[8:16]
            key, iv = _evp_bytes_to_key(passphrase, salt)
            plaintext = _decrypt_aes_cbc(ciphertext=raw[16:], key=key, iv=iv)
        decoded = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CookieCloudError("COOKIECLOUD_DECRYPT_FAILED", retryable=False) from exc
    return parse_cookiecloud_payload(decoded)


def parse_cookiecloud_payload(value: object) -> CookieCloudPayload:
    if not isinstance(value, dict):
        raise CookieCloudError("COOKIECLOUD_INVALID_PAYLOAD", retryable=False)
    cookie_data = value.get("cookie_data")
    local_storage = value.get("local_storage_data", {})
    update_time = value.get("update_time")
    if not isinstance(cookie_data, dict) or not isinstance(local_storage, dict):
        raise CookieCloudError("COOKIECLOUD_INVALID_PAYLOAD", retryable=False)
    if update_time is not None and not isinstance(update_time, str):
        update_time = None

    parsed: dict[str, tuple[CookieCloudCookie, ...]] = {}
    for raw_domain, raw_items in cookie_data.items():
        if not isinstance(raw_domain, str) or not isinstance(raw_items, list):
            continue
        items: list[CookieCloudCookie] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            name = raw_item.get("name")
            cookie_value = raw_item.get("value")
            if not isinstance(name, str) or not name or not isinstance(cookie_value, str):
                continue
            domain = raw_item.get("domain")
            path = raw_item.get("path", "/")
            expiration = raw_item.get("expirationDate")
            if not isinstance(domain, str):
                domain = raw_domain
            if not isinstance(path, str):
                path = "/"
            expiration_date = float(expiration) if isinstance(expiration, (int, float)) else None
            items.append(
                CookieCloudCookie(
                    name=name,
                    value=cookie_value,
                    domain=domain,
                    path=path,
                    expiration_date=expiration_date,
                )
            )
        parsed[raw_domain] = tuple(items)
    return CookieCloudPayload(
        cookie_data=parsed,
        local_storage_data=cast(dict[str, Any], local_storage),
        update_time=update_time,
    )


def _evp_bytes_to_key(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    if len(salt) != 8:
        raise ValueError("invalid legacy salt")
    derived = b""
    previous = b""
    while len(derived) < 48:
        previous = hashlib.md5(  # noqa: S324 - easychen/CookieCloud legacy EVP compatibility
            previous + password + salt,
            usedforsecurity=False,
        ).digest()
        derived += previous
    return derived[:32], derived[32:48]


def _infer_cookiecloud_crypto_type(encrypted: str) -> CookieCloudCryptoType:
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (ValueError, binascii.Error):
        # Preserve the existing error boundary: malformed ciphertext is
        # reported by the decrypt step, not misclassified as an unsupported
        # future algorithm.
        return CookieCloudCryptoType.LEGACY
    if raw.startswith(b"Salted__"):
        return CookieCloudCryptoType.LEGACY
    return CookieCloudCryptoType.AES_128_CBC_FIXED


def _decrypt_aes_cbc(*, ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("invalid AES-CBC ciphertext length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()
