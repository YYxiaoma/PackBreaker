from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretKindMismatch, SecretNotFound, SecretStore
from backend.app.application.sites import SiteService
from backend.app.domain.cookiecloud import (
    CookieCloudConnectionStatus,
    CookieCloudCryptoType,
    CookieCloudSyncStatus,
    cookie_domain_matches_host,
    cookie_header_for_host,
    normalize_cookiecloud_server_url,
    normalize_cookiecloud_uuid,
)
from backend.app.domain.site_config import (
    SiteCredentialKind,
    SiteKind,
    site_profiles,
)
from backend.app.domain.task_definition import normalize_cron_expression
from backend.app.infrastructure.cookiecloud import (
    CookieCloudClient,
    CookieCloudError,
    decrypt_cookiecloud_payload,
)
from backend.app.infrastructure.persistence.cookiecloud_repositories import (
    CookieCloudSettingRepository,
)
from backend.app.infrastructure.persistence.models import CookieCloudSetting

_PASSWORD_SECRET_KIND = "COOKIECLOUD_PASSWORD"
_logger = logging.getLogger("packbreaker.cookiecloud_sync")


@dataclass(frozen=True, slots=True)
class CookieCloudSettingView:
    enabled: bool
    server_url: str
    uuid: str
    password_configured: bool
    auto_sync: bool
    sync_cron_expression: str
    request_timeout_seconds: int
    connection_status: CookieCloudConnectionStatus
    last_test_at: datetime | None
    last_sync_at: datetime | None
    last_sync_status: CookieCloudSyncStatus
    last_sync_error_code: str | None
    source_domains: int
    source_cookies: int
    eligible_sites: int
    matched_sites: int
    updated_sites: int
    unchanged_sites: int
    unmatched_domains: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CookieCloudSettingUpdate:
    enabled: bool
    server_url: str
    uuid: str
    auto_sync: bool
    sync_cron_expression: str
    request_timeout_seconds: int
    password_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    password: str | None = None


@dataclass(frozen=True, slots=True)
class CookieCloudProbeView:
    tested_at: datetime
    crypto_type: CookieCloudCryptoType
    domain_count: int
    cookie_count: int
    update_time: str | None


@dataclass(frozen=True, slots=True)
class CookieCloudSyncView:
    synced_at: datetime
    crypto_type: CookieCloudCryptoType
    source_domains: int
    source_cookies: int
    eligible_sites: int
    matched_sites: int
    updated_sites: int
    unchanged_sites: int
    unmatched_domains: int
    update_time: str | None
    created_sites: int = 0
    skipped_api_key_sites: tuple[str, ...] = ()


class CookieCloudService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        client: CookieCloudClient | None = None,
        site_service: SiteService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._client = client or CookieCloudClient()
        self._site_service = site_service

    def ensure_default(self) -> CookieCloudSettingView:
        with self._session_factory() as session:
            record = CookieCloudSettingRepository(session).create_default()
            session.commit()
            session.refresh(record)
            return self._view(record)

    def get(self) -> CookieCloudSettingView:
        with self._session_factory() as session:
            repository = CookieCloudSettingRepository(session)
            record = repository.get() or repository.create_default()
            session.commit()
            session.refresh(record)
            return self._view(record)

    def update(
        self,
        change: CookieCloudSettingUpdate,
        *,
        expected_version: int,
    ) -> CookieCloudSettingView:
        server_url = self._normalize_server_url(change.server_url)
        uuid = self._normalize_uuid(change.uuid)
        sync_cron_expression = self._normalize_cron(change.sync_cron_expression)
        self._validate_runtime(change.request_timeout_seconds)
        password = self._normalize_password(change.password)
        if change.password_action == "SET" and password is None:
            raise self._invalid("设置 CookieCloud 密码时必须提供非空值")
        if change.password_action != "SET" and change.password is not None:
            raise self._invalid("只有 SET 动作可以携带 CookieCloud 密码")

        with self._session_factory() as session:
            repository = CookieCloudSettingRepository(session)
            record = repository.lock_current() or repository.create_default()
            if record.version != expected_version:
                raise self._version_conflict()
            old_secret_id = record.password_secret_id
            new_secret_id = old_secret_id
            password_changed = False
            if change.password_action == "SET":
                assert password is not None
                new_secret_id = self._secret_store.put_in_session(
                    session,
                    kind=_PASSWORD_SECRET_KIND,
                    value=password.encode("utf-8"),
                )
                password_changed = True
            elif change.password_action == "CLEAR":
                new_secret_id = None
                password_changed = old_secret_id is not None

            connection_changed = (
                record.server_url != server_url
                or record.uuid != uuid
                or record.request_timeout_seconds != change.request_timeout_seconds
                or password_changed
            )
            if change.enabled and (not server_url or not uuid or new_secret_id is None):
                raise self._invalid("启用 CookieCloud 前必须完整配置服务器地址、UUID 和密码")

            record.enabled = change.enabled
            record.server_url = server_url
            record.uuid = uuid
            record.password_secret_id = new_secret_id
            record.auto_sync = change.auto_sync
            record.sync_cron_expression = sync_cron_expression
            record.request_timeout_seconds = change.request_timeout_seconds
            if connection_changed:
                record.connection_status = CookieCloudConnectionStatus.UNTESTED.value
                record.last_test_at = None
            record.version += 1
            record.updated_at = datetime.now(UTC)
            session.flush()
            if old_secret_id is not None and old_secret_id != new_secret_id:
                self._secret_store.delete_in_session(session, old_secret_id)
            session.commit()
            session.refresh(record)
            return self._view(record)

    async def probe_saved(self) -> tuple[CookieCloudSettingView, CookieCloudProbeView]:
        with self._session_factory() as session:
            repository = CookieCloudSettingRepository(session)
            record = repository.get()
            if record is None:
                raise self._invalid("尚未保存 CookieCloud 配置")
            snapshot = (
                record.version,
                record.server_url,
                record.uuid,
                record.password_secret_id,
                record.request_timeout_seconds,
            )
        version, server_url, uuid, secret_id, timeout_seconds = snapshot
        if not server_url or not uuid or secret_id is None:
            raise self._invalid("测试 CookieCloud 前必须完整配置服务器地址、UUID 和密码")
        password = self._load_password(secret_id)
        tested_at = datetime.now(UTC)
        try:
            envelope = await self._client.fetch(
                server_url=server_url,
                uuid=uuid,
                timeout_seconds=timeout_seconds,
            )
            payload = decrypt_cookiecloud_payload(
                uuid=uuid,
                password=password,
                encrypted=envelope.encrypted,
                crypto_type=envelope.crypto_type,
            )
        except CookieCloudError as exc:
            self._store_probe(
                expected_version=version,
                status=CookieCloudConnectionStatus.FAILED,
                tested_at=tested_at,
            )
            raise self._map_error(exc) from None

        view = self._store_probe(
            expected_version=version,
            status=CookieCloudConnectionStatus.OK,
            tested_at=tested_at,
        )
        return (
            view,
            CookieCloudProbeView(
                tested_at=tested_at,
                crypto_type=envelope.crypto_type,
                domain_count=len(payload.cookie_data),
                cookie_count=sum(len(items) for items in payload.cookie_data.values()),
                update_time=payload.update_time,
            ),
        )

    async def sync_now(self) -> tuple[CookieCloudSettingView, CookieCloudSyncView]:
        if self._site_service is None:
            raise RuntimeError("CookieCloud 站点同步服务尚未初始化")
        with self._session_factory() as session:
            record = CookieCloudSettingRepository(session).get()
            if record is None:
                raise self._invalid("尚未保存 CookieCloud 配置")
            snapshot = (
                record.version,
                record.server_url,
                record.uuid,
                record.password_secret_id,
                record.request_timeout_seconds,
            )
        version, server_url, uuid, secret_id, timeout_seconds = snapshot
        if not server_url or not uuid or secret_id is None:
            raise self._invalid("同步 CookieCloud 前必须完整配置服务器地址、UUID 和密码")
        password = self._load_password(secret_id)
        synced_at = datetime.now(UTC)
        try:
            envelope = await self._client.fetch(
                server_url=server_url,
                uuid=uuid,
                timeout_seconds=timeout_seconds,
            )
            payload = decrypt_cookiecloud_payload(
                uuid=uuid,
                password=password,
                encrypted=envelope.encrypted,
                crypto_type=envelope.crypto_type,
            )
            self._require_current_version(version)
            sites = self._site_service.list_sites()
            configured_kinds = {site.type for site in sites}
            created_sites = 0
            created_cookie_sites = 0
            skipped_api_key_sites: list[str] = []
            # Match only the fixed, reviewed PackBreaker profile registry.
            # Do not automatically create or overwrite API key credentials:
            # CookieCloud contains cookies, not API keys.
            for profile in site_profiles():
                if profile.kind in configured_kinds:
                    continue
                host = urlsplit(profile.base_url).hostname
                if not host:
                    continue
                header = cookie_header_for_host(payload, host)
                # M-TEAM's API host is kp.m-team.cc, while browser cookies
                # may have been exported for a sibling *.m-team.cc domain.
                # Detect that verified domain family for a site *placeholder*
                # only. Never reuse the browser Cookie as an API token.
                mteam_cookie_present = profile.kind is SiteKind.MTEAM and any(
                    cookie_domain_matches_host("m-team.cc", cookie.domain or domain)
                    for domain, cookies in payload.cookie_data.items()
                    for cookie in cookies
                    if not cookie.expired
                )
                if header is None and not mteam_cookie_present:
                    continue
                if profile.credential_kind is not SiteCredentialKind.COOKIE:
                    skipped_api_key_sites.append(profile.display_name)
                    if profile.kind is SiteKind.MTEAM:
                        # Import a disabled, no-secret profile so it is visible
                        # in 站点管理. The API adapter still requires a genuine
                        # API Key before the site can be enabled or searched.
                        self._site_service.create(
                            name=profile.display_name,
                            kind=profile.kind,
                            credential_kind=None,
                            credential=None,
                        )
                        created_sites += 1
                    continue
                self._site_service.create(
                    name=profile.display_name,
                    kind=profile.kind,
                    credential_kind=SiteCredentialKind.COOKIE,
                    credential=header,
                    request_timeout_seconds=profile.request_timeout_seconds,
                    search_interval_seconds=int(profile.search_interval_seconds),
                )
                created_sites += 1
                created_cookie_sites += 1
            sites = self._site_service.list_sites()
            targets: dict[str, tuple[Literal["PRIMARY", "DOWNLOAD"], str]] = {}
            target_hosts: list[str] = []
            eligible_sites = 0
            for site in sites:
                host = urlsplit(site.base_url).hostname
                if host is None:
                    continue
                if (
                    site.credential_kind is not SiteCredentialKind.COOKIE
                    and site.type is not SiteKind.ROUSI_PRO
                ):
                    continue
                eligible_sites += 1
                target_hosts.append(host)
                header = cookie_header_for_host(payload, host)
                if header is None:
                    continue
                target: Literal["PRIMARY", "DOWNLOAD"] = (
                    "DOWNLOAD" if site.type is SiteKind.ROUSI_PRO else "PRIMARY"
                )
                targets[site.id] = (target, header)

            unmatched_domains = 0
            for grouped_domain, cookies in payload.cookie_data.items():
                matched = any(
                    cookie_domain_matches_host(cookie.domain or grouped_domain, host)
                    for cookie in cookies
                    if not cookie.expired
                    for host in target_hosts
                )
                if not matched:
                    unmatched_domains += 1
            source_domains = len(payload.cookie_data)
            source_cookies = sum(len(items) for items in payload.cookie_data.values())
            updated_sites = self._site_service.sync_cookiecloud_credentials(targets)
            unchanged_sites = max(0, len(targets) - updated_sites - created_cookie_sites)
        except CookieCloudError as exc:
            self._store_sync_failure(
                expected_version=version,
                synced_at=synced_at,
                error_code=exc.code,
            )
            raise self._map_error(exc) from None
        except ApplicationError as exc:
            self._store_sync_failure(
                expected_version=version,
                synced_at=synced_at,
                error_code=exc.code,
            )
            raise

        view = self._store_sync_success(
            expected_version=version,
            synced_at=synced_at,
            source_domains=source_domains,
            source_cookies=source_cookies,
            eligible_sites=eligible_sites,
            matched_sites=len(targets),
            updated_sites=updated_sites + created_cookie_sites,
            unchanged_sites=unchanged_sites,
            unmatched_domains=unmatched_domains,
        )
        _logger.info(
            "CookieCloud 同步诊断：源域名=%d，Cookie数量=%d，候选站点=%d，匹配站点=%d，"
            "新增=%d，更新=%d，未变化=%d，需独立API Key=%d，未匹配域名=%d",
            source_domains,
            source_cookies,
            eligible_sites,
            len(targets),
            created_sites,
            updated_sites,
            unchanged_sites,
            len(skipped_api_key_sites),
            unmatched_domains,
        )
        return (
            view,
            CookieCloudSyncView(
                synced_at=synced_at,
                crypto_type=envelope.crypto_type,
                source_domains=source_domains,
                source_cookies=source_cookies,
                eligible_sites=eligible_sites,
                matched_sites=len(targets),
                updated_sites=updated_sites + created_cookie_sites,
                unchanged_sites=unchanged_sites,
                unmatched_domains=unmatched_domains,
                update_time=payload.update_time,
                created_sites=created_sites,
                skipped_api_key_sites=tuple(skipped_api_key_sites),
            ),
        )

    def _store_probe(
        self,
        *,
        expected_version: int,
        status: CookieCloudConnectionStatus,
        tested_at: datetime,
    ) -> CookieCloudSettingView:
        with self._session_factory() as session:
            repository = CookieCloudSettingRepository(session)
            record = repository.lock_current()
            if record is None or record.version != expected_version:
                raise self._version_conflict()
            record.connection_status = status.value
            record.last_test_at = tested_at
            record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            return self._view(record)

    def _store_sync_success(
        self,
        *,
        expected_version: int,
        synced_at: datetime,
        source_domains: int,
        source_cookies: int,
        eligible_sites: int,
        matched_sites: int,
        updated_sites: int,
        unchanged_sites: int,
        unmatched_domains: int,
    ) -> CookieCloudSettingView:
        with self._session_factory() as session:
            repository = CookieCloudSettingRepository(session)
            record = repository.lock_current()
            if record is None or record.version != expected_version:
                raise self._version_conflict()
            record.connection_status = CookieCloudConnectionStatus.OK.value
            record.last_sync_at = synced_at
            record.last_sync_status = CookieCloudSyncStatus.SUCCESS.value
            record.last_sync_error_code = None
            record.source_domains = source_domains
            record.source_cookies = source_cookies
            record.eligible_sites = eligible_sites
            record.matched_sites = matched_sites
            record.updated_sites = updated_sites
            record.unchanged_sites = unchanged_sites
            record.unmatched_domains = unmatched_domains
            record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            return self._view(record)

    def _store_sync_failure(
        self,
        *,
        expected_version: int,
        synced_at: datetime,
        error_code: str,
    ) -> None:
        with self._session_factory() as session:
            record = CookieCloudSettingRepository(session).lock_current()
            if record is None or record.version != expected_version:
                return
            record.connection_status = CookieCloudConnectionStatus.FAILED.value
            record.last_sync_at = synced_at
            record.last_sync_status = CookieCloudSyncStatus.FAILED.value
            record.last_sync_error_code = error_code[:64]
            record.updated_at = datetime.now(UTC)
            session.commit()

    def _require_current_version(self, expected_version: int) -> None:
        with self._session_factory() as session:
            record = CookieCloudSettingRepository(session).get()
            if record is None or record.version != expected_version:
                raise self._version_conflict()

    def _load_password(self, secret_id: str) -> str:
        try:
            return self._secret_store.get(
                secret_id,
                expected_kind=_PASSWORD_SECRET_KIND,
            ).decode("utf-8")
        except (SecretNotFound, SecretKindMismatch, UnicodeDecodeError):
            raise ApplicationError(
                code="COOKIECLOUD_CONFIG_INVALID",
                status=409,
                title="CookieCloud 配置无效",
                detail="CookieCloud 密码引用无效，请重新保存密码",
            ) from None

    @staticmethod
    def _view(record: CookieCloudSetting) -> CookieCloudSettingView:
        return CookieCloudSettingView(
            enabled=record.enabled,
            server_url=record.server_url,
            uuid=record.uuid,
            password_configured=record.password_secret_id is not None,
            auto_sync=record.auto_sync,
            sync_cron_expression=record.sync_cron_expression,
            request_timeout_seconds=record.request_timeout_seconds,
            connection_status=CookieCloudConnectionStatus(record.connection_status),
            last_test_at=record.last_test_at,
            last_sync_at=record.last_sync_at,
            last_sync_status=CookieCloudSyncStatus(record.last_sync_status),
            last_sync_error_code=record.last_sync_error_code,
            source_domains=record.source_domains,
            source_cookies=record.source_cookies,
            eligible_sites=record.eligible_sites,
            matched_sites=record.matched_sites,
            updated_sites=record.updated_sites,
            unchanged_sites=record.unchanged_sites,
            unmatched_domains=record.unmatched_domains,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @classmethod
    def _normalize_server_url(cls, value: str) -> str:
        if not value.strip():
            return ""
        try:
            return normalize_cookiecloud_server_url(value)
        except ValueError as exc:
            raise cls._invalid(str(exc)) from exc

    @classmethod
    def _normalize_uuid(cls, value: str) -> str:
        if not value.strip():
            return ""
        try:
            return normalize_cookiecloud_uuid(value)
        except ValueError as exc:
            raise cls._invalid(str(exc)) from exc

    @staticmethod
    def _normalize_password(value: str | None) -> str | None:
        if value is None:
            return None
        if not value or len(value) > 512 or any(char in value for char in ("\r", "\n", "\x00")):
            raise CookieCloudService._invalid("CookieCloud 密码格式无效")
        return value

    @classmethod
    def _normalize_cron(cls, value: str) -> str:
        try:
            return normalize_cron_expression(value)
        except ValueError as exc:
            raise cls._invalid(str(exc)) from exc

    @staticmethod
    def _validate_runtime(request_timeout_seconds: int) -> None:
        if not 1 <= request_timeout_seconds <= 120:
            raise CookieCloudService._invalid("CookieCloud 请求超时必须在 1～120 秒之间")

    @staticmethod
    def _map_error(error: CookieCloudError) -> ApplicationError:
        messages = {
            "COOKIECLOUD_TIMEOUT": "CookieCloud 请求超时",
            "COOKIECLOUD_CONNECTION_FAILED": "无法连接 CookieCloud 服务器",
            "COOKIECLOUD_REDIRECT_REJECTED": "CookieCloud 服务器返回了不允许的重定向",
            "COOKIECLOUD_UUID_NOT_FOUND": "CookieCloud 未找到对应 UUID",
            "COOKIECLOUD_SERVER_ERROR": "CookieCloud 服务器暂时不可用",
            "COOKIECLOUD_REQUEST_FAILED": "CookieCloud 请求失败",
            "COOKIECLOUD_RESPONSE_TOO_LARGE": "CookieCloud 返回数据超过安全上限",
            "COOKIECLOUD_INVALID_RESPONSE": "CookieCloud 返回格式无效",
            "COOKIECLOUD_CRYPTO_UNSUPPORTED": "CookieCloud 使用了当前不支持的加密格式",
            "COOKIECLOUD_PASSWORD_INVALID": "CookieCloud 密码配置无效",
            "COOKIECLOUD_DECRYPT_FAILED": "CookieCloud 数据解密失败，请检查 UUID 与密码",
            "COOKIECLOUD_INVALID_PAYLOAD": "CookieCloud 解密后的数据格式无效",
        }
        return ApplicationError(
            code=error.code,
            status=502 if error.retryable else 422,
            title="CookieCloud 测试失败",
            detail=messages.get(error.code, "CookieCloud 请求失败"),
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="COOKIECLOUD_CONFIG_INVALID",
            status=422,
            title="CookieCloud 配置无效",
            detail=detail,
        )

    @staticmethod
    def _version_conflict() -> ApplicationError:
        return ApplicationError(
            code="COOKIECLOUD_VERSION_CONFLICT",
            status=412,
            title="CookieCloud 配置版本冲突",
            detail="CookieCloud 配置已被其他请求修改，请刷新后重试",
        )
