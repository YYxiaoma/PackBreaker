from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit


class SiteKind(StrEnum):
    MTEAM = "MTEAM"


class SiteProbeStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


def normalize_site_base_url(kind: SiteKind, value: str) -> str:
    if kind is not SiteKind.MTEAM:
        raise ValueError("暂不支持该站点类型")
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ValueError("站点 API base URL 无效") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("站点 API base URL 必须是无凭证、无路径的 HTTPS origin")
    return f"https://{parsed.netloc}"
