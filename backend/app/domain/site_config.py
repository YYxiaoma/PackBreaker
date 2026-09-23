from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class SiteKind(StrEnum):
    MTEAM = "MTEAM"
    HDTIME = "HDTIME"
    HHCLUB = "HHCLUB"
    KEEPFRDS = "KEEPFRDS"
    HDHOME = "HDHOME"
    UBITS = "UBITS"
    HDFANS = "HDFANS"
    BTSCHOOL = "BTSCHOOL"
    PTTIME = "PTTIME"
    ROUSI_PRO = "ROUSI_PRO"
    LINGYIN_CLUB = "LINGYIN_CLUB"


class SiteCredentialKind(StrEnum):
    API_KEY = "API_KEY"
    COOKIE = "COOKIE"


class SiteProbeStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


class SiteSupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PENDING_ADAPTER = "PENDING_ADAPTER"
    PENDING_REAL_VALIDATION = "PENDING_REAL_VALIDATION"


@dataclass(frozen=True, slots=True)
class SiteProfile:
    kind: SiteKind
    display_name: str
    base_url: str
    credential_kind: SiteCredentialKind
    request_timeout_seconds: int = 15
    search_interval_seconds: float = 0.0
    supports_user_agent: bool = False
    supports_browser_emulation: bool = False
    supports_proxy: bool = True
    support_status: SiteSupportStatus = SiteSupportStatus.SUPPORTED

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("站点 Profile 展示名称不能为空")
        if not 1 <= self.request_timeout_seconds <= 120:
            raise ValueError("站点 Profile 请求超时必须在 1～120 秒之间")
        if not 0 <= self.search_interval_seconds <= 3600:
            raise ValueError("站点 Profile 搜索间隔必须在 0～3600 秒之间")


SITE_PROFILE_REGISTRY: dict[SiteKind, SiteProfile] = {
    SiteKind.MTEAM: SiteProfile(
        kind=SiteKind.MTEAM,
        display_name="M-TEAM",
        base_url="https://kp.m-team.cc",
        credential_kind=SiteCredentialKind.API_KEY,
    ),
    SiteKind.HDTIME: SiteProfile(
        kind=SiteKind.HDTIME,
        display_name="HDTime",
        base_url="https://hdtime.org",
        credential_kind=SiteCredentialKind.COOKIE,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.HHCLUB: SiteProfile(
        kind=SiteKind.HHCLUB,
        display_name="HHClub",
        base_url="https://hhanclub.net",
        credential_kind=SiteCredentialKind.COOKIE,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.KEEPFRDS: SiteProfile(
        kind=SiteKind.KEEPFRDS,
        display_name="KeepFrds",
        base_url="https://pt.keepfrds.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.HDHOME: SiteProfile(
        kind=SiteKind.HDHOME,
        display_name="HDHome",
        base_url="https://hdhome.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.UBITS: SiteProfile(
        kind=SiteKind.UBITS,
        display_name="UBits",
        base_url="https://ubits.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.HDFANS: SiteProfile(
        kind=SiteKind.HDFANS,
        display_name="HDFans",
        base_url="https://hdfans.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.BTSCHOOL: SiteProfile(
        kind=SiteKind.BTSCHOOL,
        display_name="BTSCHOOL",
        base_url="https://pt.btschool.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.PTTIME: SiteProfile(
        kind=SiteKind.PTTIME,
        display_name="PTTime",
        base_url="https://www.pttime.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.ROUSI_PRO: SiteProfile(
        kind=SiteKind.ROUSI_PRO,
        display_name="Rousi Pro",
        base_url="https://rousi.pro",
        credential_kind=SiteCredentialKind.API_KEY,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
    SiteKind.LINGYIN_CLUB: SiteProfile(
        kind=SiteKind.LINGYIN_CLUB,
        display_name="聆音Club",
        base_url="https://pt.soulvoice.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.PENDING_ADAPTER,
    ),
}


# Persistability is an explicit application-service security decision, not a
# consequence of the schema accepting a known enum member.
PERSISTED_SITE_KINDS = frozenset({SiteKind.MTEAM, SiteKind.HDTIME, SiteKind.HHCLUB})
# Schema capacity is independent from the production create/enable allowlist.
# Merely accepting a type at the database layer must never enable its adapter.
DATABASE_SITE_KINDS = frozenset(SiteKind)
PERSISTED_SITE_CREDENTIAL_KINDS = frozenset({SiteCredentialKind.API_KEY, SiteCredentialKind.COOKIE})


DEFAULT_COOKIE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def site_profiles() -> tuple[SiteProfile, ...]:
    return tuple(SITE_PROFILE_REGISTRY[kind] for kind in SiteKind if kind in SITE_PROFILE_REGISTRY)


def site_profile(kind: SiteKind) -> SiteProfile:
    try:
        return SITE_PROFILE_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError("站点类型尚未注册 Profile") from exc


def trusted_site_base_url(kind: SiteKind) -> str:
    return site_profile(kind).base_url


def site_kind_is_persistable(kind: SiteKind) -> bool:
    return (
        kind in PERSISTED_SITE_KINDS
        and site_profile(kind).support_status is SiteSupportStatus.SUPPORTED
    )


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
    if kind is SiteKind.MTEAM and (
        port not in {None, 443} or not (host == "m-team.cc" or host.endswith(".m-team.cc"))
    ):
        raise ValueError("M-Team 站点地址必须位于 https://*.m-team.cc")
    netloc = host if port in {None, 443} else f"{host}:{port}"
    normalized = f"https://{netloc}"
    if kind is not SiteKind.MTEAM and normalized != trusted_site_base_url(kind):
        raise ValueError(f"{site_profile(kind).display_name} 站点地址必须是受信任 Profile 固定地址")
    return normalized


def required_site_credential_kind(kind: SiteKind) -> SiteCredentialKind:
    return site_profile(kind).credential_kind


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
