import pytest

from backend.app.domain.notification import NotificationEventType
from backend.app.domain.proxy import ProxyConfig
from backend.app.domain.site_config import (
    CONFIGURABLE_SITE_KINDS,
    PERSISTED_SITE_KINDS,
    SITE_PROFILE_REGISTRY,
    SiteCredentialKind,
    SiteKind,
    SiteSupportStatus,
    required_site_credential_kind,
    trusted_site_base_url,
)


def test_site_profile_registry_lists_supported_and_planned_origins() -> None:
    assert set(SITE_PROFILE_REGISTRY) == set(SiteKind)
    supported = {
        kind
        for kind in SiteKind
        if SITE_PROFILE_REGISTRY[kind].support_status is SiteSupportStatus.SUPPORTED
    }
    assert supported == PERSISTED_SITE_KINDS
    assert len(set(SiteKind) - PERSISTED_SITE_KINDS) == len(SiteKind) - len(PERSISTED_SITE_KINDS)
    assert frozenset(SiteKind) == CONFIGURABLE_SITE_KINDS
    assert trusted_site_base_url(SiteKind.MTEAM) == "https://kp.m-team.cc"
    assert trusted_site_base_url(SiteKind.HDTIME) == "https://hdtime.org"
    assert trusted_site_base_url(SiteKind.HHCLUB) == "https://hhanclub.net"
    assert trusted_site_base_url(SiteKind.KEEPFRDS) == "https://pt.keepfrds.com"
    assert trusted_site_base_url(SiteKind.HDHOME) == "https://hdhome.org"
    assert trusted_site_base_url(SiteKind.HDDOLBY) == "https://www.uhddolby.com"
    assert trusted_site_base_url(SiteKind.UBITS) == "https://ubits.club"
    assert trusted_site_base_url(SiteKind.HDFANS) == "https://hdfans.org"
    assert trusted_site_base_url(SiteKind.BTSCHOOL) == "https://pt.btschool.club"
    assert trusted_site_base_url(SiteKind.PTTIME) == "https://www.pttime.org"
    assert trusted_site_base_url(SiteKind.ROUSI_PRO) == "https://rousi.pro"
    assert required_site_credential_kind(SiteKind.MTEAM) is SiteCredentialKind.API_KEY
    assert required_site_credential_kind(SiteKind.HHCLUB) is SiteCredentialKind.COOKIE
    assert required_site_credential_kind(SiteKind.ROUSI_PRO) is SiteCredentialKind.API_KEY
    # v1.1.18 起新增站点全部开放，注册表内所有类型都是 SUPPORTED 且可执行。
    assert frozenset(SiteKind) == PERSISTED_SITE_KINDS
    assert all(
        profile.support_status is SiteSupportStatus.SUPPORTED
        for profile in SITE_PROFILE_REGISTRY.values()
    )
    assert SITE_PROFILE_REGISTRY[SiteKind.CHDBITS].base_url == "https://ptchdbits.co"
    assert SITE_PROFILE_REGISTRY[SiteKind.OURBITS].base_url == "https://ourbits.club"
    assert SiteKind.OURBITS in PERSISTED_SITE_KINDS
    assert SITE_PROFILE_REGISTRY[SiteKind.OURBITS].support_status is SiteSupportStatus.SUPPORTED
    assert SiteKind.BYRBT in PERSISTED_SITE_KINDS
    assert SITE_PROFILE_REGISTRY[SiteKind.BYRBT].support_status is SiteSupportStatus.SUPPORTED


def test_site_display_names_are_unique() -> None:
    # 站点名称参与唯一约束；CookieCloud 等批量导入会按 display_name 建站，
    # 重名会让整批同步以 SITE_NAME_CONFLICT 失败。
    names = [profile.display_name for profile in SITE_PROFILE_REGISTRY.values()]
    assert len(names) == len(set(names))


def test_cookie_profiles_support_controlled_request_headers() -> None:
    assert SITE_PROFILE_REGISTRY[SiteKind.HDTIME].supports_user_agent is True
    assert SITE_PROFILE_REGISTRY[SiteKind.HDTIME].supports_browser_emulation is True
    assert SITE_PROFILE_REGISTRY[SiteKind.HHCLUB].supports_user_agent is True
    assert SITE_PROFILE_REGISTRY[SiteKind.HHCLUB].supports_proxy is True
    assert SITE_PROFILE_REGISTRY[SiteKind.MTEAM].supports_proxy is True


def test_proxy_config_requires_binding_only_when_enabled() -> None:
    assert ProxyConfig().enabled is False
    assert ProxyConfig(enabled=True, host="proxy.internal", port=8080).host == "proxy.internal"
    with pytest.raises(ValueError, match="地址和端口"):
        ProxyConfig(enabled=True, host="proxy.internal")
    with pytest.raises(ValueError, match="1～65535"):
        ProxyConfig(host="proxy.internal", port=70000)


def test_notification_event_types_include_v016_product_events() -> None:
    assert {
        NotificationEventType.AUTH_LOGIN_SUCCESS,
        NotificationEventType.AUTH_PASSWORD_CHANGED,
        NotificationEventType.TASK_EXECUTION_RESULT,
        NotificationEventType.DOWNLOADER_CREATED,
        NotificationEventType.SITE_CREATED,
    }.issubset(set(NotificationEventType))
