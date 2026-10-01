from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit


class CookieCloudCryptoType(StrEnum):
    LEGACY = "legacy"
    AES_128_CBC_FIXED = "aes-128-cbc-fixed"


class CookieCloudConnectionStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


class CookieCloudSyncStatus(StrEnum):
    NEVER = "NEVER"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CookieCloudCookie:
    name: str
    value: str
    domain: str
    path: str
    expiration_date: float | None

    @property
    def expired(self) -> bool:
        return (
            self.expiration_date is not None
            and self.expiration_date <= datetime.now(UTC).timestamp()
        )


@dataclass(frozen=True, slots=True)
class CookieCloudPayload:
    cookie_data: dict[str, tuple[CookieCloudCookie, ...]]
    local_storage_data: dict[str, Any]
    update_time: str | None


def normalize_cookiecloud_server_url(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 2048 or any(char in raw for char in ("\r", "\n", "\x00")):
        raise ValueError("CookieCloud 服务器地址无效")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("CookieCloud 服务器地址无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CookieCloud 服务器地址必须是无凭证、无查询参数的 HTTP(S) 地址")
    return raw.rstrip("/")


def normalize_cookiecloud_uuid(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 255
        or any(char in normalized for char in ("\r", "\n", "\x00"))
    ):
        raise ValueError("CookieCloud UUID 无效")
    return normalized


def cookie_domain_matches_host(cookie_domain: str, request_host: str) -> bool:
    domain = cookie_domain.strip().lower().lstrip(".").rstrip(".")
    host = request_host.strip().lower().rstrip(".")
    if not domain or not host:
        return False
    return host == domain or host.endswith(f".{domain}")


def cookie_header_for_host(payload: CookieCloudPayload, host: str) -> str | None:
    pairs: list[str] = []
    seen: set[tuple[str, str]] = set()
    for grouped_domain, cookies in payload.cookie_data.items():
        for cookie in cookies:
            effective_domain = cookie.domain or grouped_domain
            if cookie.expired or not cookie_domain_matches_host(effective_domain, host):
                continue
            pair = (cookie.name, cookie.value)
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(f"{cookie.name}={cookie.value}")
    return "; ".join(pairs) or None
