from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit


class SiteKind(StrEnum):
    MTEAM = "MTEAM"
    HDTIME = "HDTIME"


class SiteCredentialKind(StrEnum):
    API_KEY = "API_KEY"
    COOKIE = "COOKIE"


class SiteProbeStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


def normalize_site_base_url(kind: SiteKind, value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("站点 base URL 无效") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("站点 base URL 必须是无凭证、无路径的 HTTPS origin")
    host = parsed.hostname.casefold().rstrip(".")
    if not host or ":" in host:
        raise ValueError("站点 base URL host 无效")
    if kind is SiteKind.HDTIME and (host != "hdtime.org" or port not in {None, 443}):
        raise ValueError("HDTime 站点地址必须是 https://hdtime.org")
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return f"https://{netloc}"


def required_site_credential_kind(kind: SiteKind) -> SiteCredentialKind:
    if kind is SiteKind.MTEAM:
        return SiteCredentialKind.API_KEY
    if kind is SiteKind.HDTIME:
        return SiteCredentialKind.COOKIE
    raise ValueError("暂不支持该站点类型")


def normalize_site_credential(kind: SiteCredentialKind, value: str) -> str:
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("站点凭证不能包含 NUL 或换行符")
    if kind is SiteCredentialKind.API_KEY:
        normalized = value.strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("API Key 不能为空且最长 512 个字符")
        return normalized
    if kind is SiteCredentialKind.COOKIE:
        if not value.strip() or len(value) > 8192:
            raise ValueError("Cookie 不能为空且最长 8192 个字符")
        return value
    raise ValueError("未知站点凭证类型")
