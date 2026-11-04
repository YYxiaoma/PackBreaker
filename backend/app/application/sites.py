from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from time import perf_counter
from typing import Any, Literal
from urllib.parse import quote

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretKindMismatch, SecretNotFound, SecretStore
from backend.app.application.unpack_acceptance import UnpackAcceptanceScope
from backend.app.domain.proxy import ProxyConfig
from backend.app.domain.site_adapter import SiteAdapter, SiteUserProfile
from backend.app.domain.site_config import (
    DEFAULT_COOKIE_USER_AGENT,
    SiteCredentialKind,
    SiteKind,
    SiteProbeStatus,
    SiteSupportStatus,
    normalize_site_base_url,
    normalize_site_credential,
    required_site_credential_kind,
    site_kind_is_configurable,
    site_kind_is_persistable,
    site_profile,
    trusted_site_base_url,
)
from backend.app.infrastructure.adapters.sites import SiteAdapterError, SiteAdapterFactory
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    Site,
    SiteRiskEvent,
    SiteUserProfileSnapshot,
    SiteUserProfileSync,
    UnpackExecution,
    new_uuid,
)
from backend.app.infrastructure.persistence.site_repositories import SiteRepository
from backend.app.infrastructure.site_reliability import (
    SiteReliabilityHealth,
    SiteReliabilityRegistry,
)

SITE_RISK_CONTROL_ERROR_CODES = frozenset(
    {
        "SITE_RATE_LIMITED",
        "SITE_ANTIBOT_BLOCKED",
        "SITE_CAPTCHA_REQUIRED",
        "SITE_CLOUDFLARE_CHALLENGE",
        "SITE_FREQUENCY_LIMITED",
    }
)


@dataclass(frozen=True, slots=True)
class SiteView:
    id: str
    name: str
    type: SiteKind
    base_url: str
    credential_kind: SiteCredentialKind
    credential_configured: bool
    download_credential_configured: bool
    request_timeout_seconds: int
    search_interval_seconds: int
    user_agent: str | None
    browser_emulation_enabled: bool
    proxy_enabled: bool
    proxy_host: str | None
    proxy_port: int | None
    proxy_username: str | None
    proxy_credential_configured: bool
    capabilities: dict[str, Any]
    support_status: SiteSupportStatus
    connection_status: SiteProbeStatus
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime
    risk_control_limit: int = 3
    risk_retry_interval_seconds: int = 60
    risk_disabled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SiteRiskControlOutcome:
    risk_count: int
    risk_limit: int
    threshold_reached: bool
    retry_interval_seconds: int
    risk_disabled_at: datetime | None


_USER_PROFILE_DATA_FIELDS = (
    "uid",
    "username",
    "user_level",
    "join_time",
    "last_access_at",
    "real_uploaded_bytes",
    "real_downloaded_bytes",
    "uploaded_bytes",
    "downloaded_bytes",
    "ratio",
    "torrents_posted",
    "seeding_count",
    "seeding_size_bytes",
    "bonus",
    "seeding_points",
    "bonus_per_hour",
    "hnr_status",
    "hnr_count",
    "message_count",
    "remark",
)


def _duration_ms(started_clock: float) -> int:
    return max(0, int(round((perf_counter() - started_clock) * 1000)))


def _user_profile_field_count(profile: SiteUserProfile) -> int:
    return sum(getattr(profile, field_name) is not None for field_name in _USER_PROFILE_DATA_FIELDS)


def _user_profile_record_values(profile: SiteUserProfile) -> dict[str, object]:
    return {
        field_name: getattr(profile, field_name) for field_name in _USER_PROFILE_DATA_FIELDS
    } | {
        "fetched_at": profile.fetched_at,
    }


@dataclass(frozen=True, slots=True)
class SiteUpdate:
    name: str | None = None
    type: SiteKind | None = None
    base_url: str | None = None
    credential_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    credential_kind: SiteCredentialKind | None = None
    credential: str | None = None
    download_cookie_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    download_cookie: str | None = None
    runtime_config: dict[str, Any] | None = None
    proxy_password_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    proxy_password: str | None = None
    risk_control_limit: int | None = None
    risk_retry_interval_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class _SiteConnectionSnapshot:
    id: str
    version: int
    enabled: bool
    type: SiteKind
    base_url: str
    credential_kind: SiteCredentialKind
    secret_id: str | None
    download_secret_id: str | None
    request_timeout_seconds: int
    search_interval_seconds: int
    user_agent: str | None
    browser_emulation_enabled: bool
    proxy_enabled: bool
    proxy_host: str | None
    proxy_port: int | None
    proxy_username: str | None
    proxy_secret_id: str | None


@dataclass(frozen=True, slots=True)
class EnabledSiteAdapter:
    config_id: str
    config_version: int
    site_id: str
    adapter: SiteAdapter
    request_timeout_seconds: int = 30
    search_interval_seconds: int = 0


@dataclass(frozen=True, slots=True)
class SiteConfigReference:
    """Non-secret local site identity for metadata-only operations."""

    config_id: str
    config_version: int
    site_id: str
    enabled: bool


class SiteService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        adapter_factory: SiteAdapterFactory | None = None,
        reliability_registry: SiteReliabilityRegistry | None = None,
        acceptance_scope: UnpackAcceptanceScope | None = None,
    ) -> None:
        self._acceptance_scope = acceptance_scope
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._adapter_factory = adapter_factory or SiteAdapterFactory()
        self._reliability_registry = reliability_registry or SiteReliabilityRegistry()
        self._profile_sync_locks: dict[str, asyncio.Lock] = {}

    def list_sites(self) -> list[SiteView]:
        with self._session_factory() as session:
            return [self._view(record) for record in SiteRepository(session).list_all()]

    def get(self, site_id: str) -> SiteView:
        with self._session_factory() as session:
            return self._view(self._require_record(SiteRepository(session), site_id))

    def detail_capabilities(self, site_id: str) -> dict[str, bool]:
        """Return backend-authoritative feature capabilities for the detail view."""
        site = self.get(site_id)
        if site.type is SiteKind.ROUSI_PRO:
            return {
                "search": True,
                "torrent_detail": False,
                "torrent_download": site.download_credential_configured,
                "user_info": True,
                "seeding_info": False,
                "api": site.credential_kind is SiteCredentialKind.API_KEY,
                "cookie": site.credential_kind is SiteCredentialKind.COOKIE,
                "browser_emulation": site_profile(site.type).supports_browser_emulation,
                "proxy": site_profile(site.type).supports_proxy,
            }
        supported = site.support_status is not SiteSupportStatus.PENDING_ADAPTER
        return {
            "search": supported,
            "torrent_detail": supported,
            "torrent_download": supported,
            "user_info": supported,
            "seeding_info": False,
            "api": site.credential_kind is SiteCredentialKind.API_KEY,
            "cookie": site.credential_kind is SiteCredentialKind.COOKIE,
            "browser_emulation": site_profile(site.type).supports_browser_emulation,
            "proxy": site_profile(site.type).supports_proxy,
        }

    def sync_cookiecloud_credentials(
        self,
        updates: Mapping[str, tuple[Literal["PRIMARY", "DOWNLOAD"], str]],
    ) -> int:
        """Atomically replace site Cookies sourced from CookieCloud.

        Enabled state is intentionally preserved so a successful background
        refresh does not interrupt task execution. API-key credentials are
        never replaced; Rousi Pro only accepts the independent download Cookie.
        """

        if not updates:
            return 0
        changed_ids: list[str] = []
        with self._session_factory() as session:
            repository = SiteRepository(session)
            for site_id, (target, raw_cookie) in sorted(updates.items()):
                current = self._require_record(repository, site_id)
                kind = SiteKind(current.type)
                if target == "PRIMARY":
                    if SiteCredentialKind(current.credential_kind) is not SiteCredentialKind.COOKIE:
                        raise self._credential_invalid(
                            "CookieCloud 不能覆盖使用 API Key 的站点主凭证"
                        )
                    secret_field = "secret_id"
                    expected_secret_kind = "SITE_COOKIE"
                else:
                    if kind is not SiteKind.ROUSI_PRO:
                        raise self._credential_invalid(
                            "CookieCloud 独立下载 Cookie 仅允许用于 Rousi Pro"
                        )
                    secret_field = "download_secret_id"
                    expected_secret_kind = "SITE_DOWNLOAD_COOKIE"

                normalized = self._normalize_credential(SiteCredentialKind.COOKIE, raw_cookie)
                old_secret_id = getattr(current, secret_field)
                if old_secret_id is not None:
                    try:
                        old_cookie = self._secret_store.get_in_session(
                            session,
                            old_secret_id,
                            expected_kind=expected_secret_kind,
                        ).decode("utf-8")
                    except (SecretNotFound, SecretKindMismatch, UnicodeDecodeError):
                        old_cookie = None
                    if old_cookie == normalized:
                        continue

                new_secret_id = self._secret_store.put_in_session(
                    session,
                    kind=expected_secret_kind,
                    value=normalized.encode(),
                )
                values: dict[str, Any] = {secret_field: new_secret_id}
                if target == "PRIMARY":
                    values.update(
                        connection_status=SiteProbeStatus.UNTESTED.value,
                        capabilities={},
                        last_test_at=None,
                    )
                if not repository.update_config(
                    site_id,
                    expected_version=current.version,
                    values=values,
                ):
                    session.rollback()
                    raise self._version_conflict()
                if old_secret_id is not None:
                    self._secret_store.delete_in_session(session, old_secret_id)
                changed_ids.append(site_id)
            session.commit()
        for site_id in changed_ids:
            self._reliability_registry.discard(site_id)
        return len(changed_ids)

    async def health(self, site_id: str) -> SiteReliabilityHealth:
        with self._session_factory() as session:
            current = self._require_record(SiteRepository(session), site_id)
            version = current.version
        return await self._reliability_registry.health(
            config_id=site_id,
            config_version=version,
        )

    async def reset_circuit(self, site_id: str, *, expected_version: int) -> SiteReliabilityHealth:
        with self._session_factory() as session:
            current = self._require_record(SiteRepository(session), site_id)
            if current.version != expected_version:
                raise self._version_conflict()
        return await self._reliability_registry.reset_circuit(
            config_id=site_id,
            config_version=expected_version,
        )

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        with self._session_factory() as session:
            records = tuple(SiteRepository(session).list_enabled())
            snapshots = tuple(self._snapshot(record) for record in records)
        # Validate *all* stored kinds before decrypting even the first secret.
        # A future unsupported row must not activate a mixed batch.
        for snapshot in snapshots:
            self._require_persistable_kind(snapshot.type)
            if snapshot.type is SiteKind.ROUSI_PRO and snapshot.download_secret_id is None:
                raise ApplicationError(
                    code="SITE_ENABLED_CONFIG_INVALID",
                    status=500,
                    title="已启用站点配置无效",
                    detail="Rousi Pro 已启用配置缺少独立下载 Cookie",
                )
        result: list[EnabledSiteAdapter] = []
        for snapshot in snapshots:
            if snapshot.secret_id is None:
                raise ApplicationError(
                    code="SITE_ENABLED_CONFIG_INVALID",
                    status=500,
                    title="已启用站点配置无效",
                    detail="已启用站点缺少凭证",
                )
            credential = self._read_primary_credential(snapshot)
            result.append(
                EnabledSiteAdapter(
                    config_id=snapshot.id,
                    config_version=snapshot.version,
                    site_id=self._expected_site_id(snapshot.type),
                    adapter=self._reliability_registry.wrap(
                        config_id=snapshot.id,
                        config_version=snapshot.version,
                        adapter=self._create_adapter(snapshot, credential),
                        min_request_interval_seconds=snapshot.search_interval_seconds,
                    ),
                    request_timeout_seconds=snapshot.request_timeout_seconds,
                    search_interval_seconds=snapshot.search_interval_seconds,
                )
            )
        return tuple(result)

    def config_references_for_site_ids(
        self, site_config_ids: tuple[str, ...]
    ) -> tuple[SiteConfigReference, ...]:
        """Resolve local site IDs without decrypting credentials or making network requests."""
        ids = tuple(dict.fromkeys(value for value in site_config_ids if value))
        if not ids:
            return ()
        with self._session_factory() as session:
            records = session.scalars(select(Site).where(Site.id.in_(ids))).all()
            by_id = {record.id: record for record in records}
            return tuple(
                SiteConfigReference(
                    config_id=record.id,
                    config_version=record.version,
                    site_id=self._expected_site_id(SiteKind(record.type)),
                    enabled=record.enabled,
                )
                for site_id in ids
                if (record := by_id.get(site_id)) is not None
            )

    def config_references_for_site_keys(
        self, site_keys: tuple[str, ...]
    ) -> tuple[SiteConfigReference, ...]:
        """Map portable adapter site keys to enabled local configurations, without secrets."""
        keys = frozenset(value for value in site_keys if value)
        if not keys:
            return ()
        with self._session_factory() as session:
            records = session.scalars(select(Site).where(Site.enabled.is_(True))).all()
            result: list[SiteConfigReference] = []
            for record in records:
                key = self._expected_site_id(SiteKind(record.type))
                if key in keys:
                    result.append(
                        SiteConfigReference(
                            config_id=record.id,
                            config_version=record.version,
                            site_id=key,
                            enabled=record.enabled,
                        )
                    )
            return tuple(result)

    def adapter_for_config(self, site_config_id: str) -> EnabledSiteAdapter:
        """Build exactly one enabled site's adapter for an explicit metadata import."""
        with self._session_factory() as session:
            record = session.get(Site, site_config_id)
            if record is None:
                raise ApplicationError(
                    code="SITE_NOT_FOUND",
                    status=404,
                    title="站点不存在",
                    detail="未找到指定站点配置",
                )
            snapshot = self._snapshot(record)
        self._require_persistable_kind(snapshot.type)
        if not snapshot.enabled:
            raise ApplicationError(
                code="SITE_DISABLED",
                status=409,
                title="站点未启用",
                detail="请先启用目标站点，再导入种子元数据",
            )
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_ENABLED_CONFIG_INVALID",
                status=500,
                title="站点配置无效",
                detail="目标站点缺少凭证",
            )
        credential = self._read_primary_credential(snapshot)
        return EnabledSiteAdapter(
            config_id=snapshot.id,
            config_version=snapshot.version,
            site_id=self._expected_site_id(snapshot.type),
            adapter=self._reliability_registry.wrap(
                config_id=snapshot.id,
                config_version=snapshot.version,
                adapter=self._create_adapter(snapshot, credential),
                min_request_interval_seconds=snapshot.search_interval_seconds,
            ),
            request_timeout_seconds=snapshot.request_timeout_seconds,
            search_interval_seconds=snapshot.search_interval_seconds,
        )

    def bindings_for_unpack_execution(self, execution_id: str) -> tuple[EnabledSiteAdapter, ...]:
        """Never promote an acceptance-only CarPT site into global enabled sites."""
        normal = self.enabled_adapters()
        if self._acceptance_scope is None:
            return normal
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None or not self._acceptance_scope.authorize_execution(
                session, execution
            ):
                return normal
            site_id = execution.config_snapshot["site_ids"][0]
            record = session.get(Site, site_id)
            if record is None:
                return normal
            snapshot = self._snapshot(record)
        if snapshot.secret_id is None:
            return normal
        credential = self._read_primary_credential(snapshot)
        return (
            EnabledSiteAdapter(
                config_id=snapshot.id,
                config_version=snapshot.version,
                site_id=self._expected_site_id(snapshot.type),
                adapter=self._reliability_registry.wrap(
                    config_id=snapshot.id,
                    config_version=snapshot.version,
                    adapter=self._create_adapter(snapshot, credential),
                    min_request_interval_seconds=snapshot.search_interval_seconds,
                ),
                request_timeout_seconds=snapshot.request_timeout_seconds,
                search_interval_seconds=snapshot.search_interval_seconds,
            ),
        )

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        with self._session_factory() as session:
            records = tuple(SiteRepository(session).list_enabled())
            for record in records:
                self._require_persistable_kind(SiteKind(record.type))
            return tuple((record.id, record.version) for record in records)

    def create(
        self,
        *,
        name: str,
        kind: SiteKind,
        base_url: str | None = None,
        credential_kind: SiteCredentialKind | None,
        credential: str | None,
        download_cookie: str | None = None,
        request_timeout_seconds: int = 15,
        search_interval_seconds: int = 0,
        risk_control_limit: int = 3,
        risk_retry_interval_seconds: int = 60,
        user_agent: str | None = None,
        browser_emulation_enabled: bool = False,
        proxy_enabled: bool = False,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
    ) -> SiteView:
        self._require_configurable_kind(kind)
        normalized_name = self._normalize_name(name)
        normalized_url = self._fixed_base_url(kind, base_url)
        required_kind = required_site_credential_kind(kind)
        risk_config = self._normalize_risk_control_config(
            risk_control_limit=risk_control_limit,
            risk_retry_interval_seconds=risk_retry_interval_seconds,
        )
        runtime = self._normalize_runtime_config(
            kind,
            request_timeout_seconds=request_timeout_seconds,
            search_interval_seconds=search_interval_seconds,
            user_agent=user_agent,
            browser_emulation_enabled=browser_emulation_enabled,
            proxy_enabled=proxy_enabled,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_username=proxy_username,
        )
        normalized_credential: str | None = None
        if credential is not None:
            if credential_kind is not required_kind:
                raise self._credential_kind_mismatch(required_kind)
            normalized_credential = self._normalize_credential(required_kind, credential)
        elif credential_kind is not None:
            raise self._credential_invalid("凭证类型不能脱离凭证值单独提交")
        normalized_download_cookie = self._validate_download_cookie(kind, download_cookie)
        normalized_proxy_password = self._normalize_proxy_password(proxy_password)
        if normalized_proxy_password is not None and not runtime["proxy_username"]:
            raise self._proxy_invalid("配置代理密码时必须同时提供代理账号")
        with self._session_factory() as session:
            secret_id = (
                self._secret_store.put_in_session(
                    session,
                    kind=self._secret_kind(required_kind),
                    value=normalized_credential.encode(),
                )
                if normalized_credential is not None
                else None
            )
            download_secret_id = (
                self._secret_store.put_in_session(
                    session,
                    kind="SITE_DOWNLOAD_COOKIE",
                    value=normalized_download_cookie.encode("utf-8"),
                )
                if normalized_download_cookie is not None
                else None
            )
            proxy_secret_id = (
                self._secret_store.put_in_session(
                    session,
                    kind="SITE_PROXY_PASSWORD",
                    value=normalized_proxy_password.encode(),
                )
                if normalized_proxy_password is not None
                else None
            )
            try:
                record = SiteRepository(session).create(
                    name=normalized_name,
                    kind=kind.value,
                    base_url=normalized_url,
                    credential_kind=required_kind.value,
                    secret_id=secret_id,
                    download_secret_id=download_secret_id,
                    request_timeout_seconds=runtime["request_timeout_seconds"],
                    search_interval_seconds=runtime["search_interval_seconds"],
                    risk_control_limit=risk_config["risk_control_limit"],
                    risk_retry_interval_seconds=risk_config["risk_retry_interval_seconds"],
                    user_agent=runtime["user_agent"],
                    browser_emulation_enabled=runtime["browser_emulation_enabled"],
                    proxy_enabled=runtime["proxy_enabled"],
                    proxy_host=runtime["proxy_host"],
                    proxy_port=runtime["proxy_port"],
                    proxy_username=runtime["proxy_username"],
                    proxy_secret_id=proxy_secret_id,
                )
                session.commit()
                session.refresh(record)
            except IntegrityError as exc:
                session.rollback()
                raise self._name_conflict() from exc
            return self._view(record)

    def update(
        self,
        site_id: str,
        *,
        expected_version: int,
        update_request: SiteUpdate,
    ) -> SiteView:
        with self._session_factory() as session:
            repository = SiteRepository(session)
            current = self._require_record(repository, site_id)
            current_kind = SiteKind(current.type)
            next_kind = update_request.type or current_kind
            self._require_configurable_kind(next_kind)
            if update_request.download_cookie_action == "SET":
                if update_request.download_cookie is None:
                    raise self._credential_invalid("下载 Cookie 不能为空")
                normalized_download_cookie = self._validate_download_cookie(
                    next_kind, update_request.download_cookie
                )
            else:
                normalized_download_cookie = None
                if update_request.download_cookie is not None:
                    raise self._credential_invalid("下载 Cookie 操作与值不匹配")
            required_kind = required_site_credential_kind(next_kind)
            if (
                update_request.type is not None
                and update_request.type is not current_kind
                and current.secret_id is not None
                and update_request.credential_action == "KEEP"
            ):
                raise ApplicationError(
                    code="SITE_CREDENTIAL_REPLACEMENT_REQUIRED",
                    status=422,
                    title="站点凭证需要更新",
                    detail="切换站点类型时必须同时替换或清除现有凭证",
                )

            values: dict[str, Any] = {}
            connection_changed = False
            if (
                update_request.risk_control_limit is not None
                or update_request.risk_retry_interval_seconds is not None
            ):
                values.update(
                    self._normalize_risk_control_config(
                        risk_control_limit=(
                            update_request.risk_control_limit
                            if update_request.risk_control_limit is not None
                            else current.risk_control_limit
                        ),
                        risk_retry_interval_seconds=(
                            update_request.risk_retry_interval_seconds
                            if update_request.risk_retry_interval_seconds is not None
                            else current.risk_retry_interval_seconds
                        ),
                    )
                )
            if update_request.name is not None:
                values["name"] = self._normalize_name(update_request.name)
            if update_request.type is not None:
                values["type"] = next_kind.value
                values["credential_kind"] = required_kind.value
                values["base_url"] = trusted_site_base_url(next_kind)
                connection_changed = True
            if update_request.base_url is not None:
                fixed_url = self._fixed_base_url(next_kind, update_request.base_url)
                if fixed_url != current.base_url:
                    values["base_url"] = fixed_url
                    connection_changed = True

            if update_request.runtime_config is not None or next_kind is not current_kind:
                runtime = self._merged_runtime_config(
                    current,
                    next_kind,
                    update_request.runtime_config or {},
                )
                runtime_changed = any(
                    getattr(current, key) != value for key, value in runtime.items()
                )
                if runtime_changed:
                    values.update(runtime)
                    connection_changed = True

            old_secret_id = current.secret_id
            if update_request.credential_action == "SET":
                if update_request.credential is None or update_request.credential_kind is None:
                    raise self._credential_invalid("新凭证必须同时包含类型和值")
                if update_request.credential_kind is not required_kind:
                    raise self._credential_kind_mismatch(required_kind)
                normalized_credential = self._normalize_credential(
                    required_kind, update_request.credential
                )
                values["secret_id"] = self._secret_store.put_in_session(
                    session,
                    kind=self._secret_kind(required_kind),
                    value=normalized_credential.encode(),
                )
                values["credential_kind"] = required_kind.value
                connection_changed = True
            elif update_request.credential_action == "CLEAR":
                values["secret_id"] = None
                values["credential_kind"] = required_kind.value
                connection_changed = True

            old_proxy_secret_id = current.proxy_secret_id
            if update_request.proxy_password_action == "SET":
                normalized_proxy_password = self._normalize_proxy_password(
                    update_request.proxy_password
                )
                if normalized_proxy_password is None:
                    raise self._proxy_invalid("代理密码不能为空")
                proxy_username = values.get("proxy_username", current.proxy_username)
                if not proxy_username:
                    raise self._proxy_invalid("配置代理密码时必须同时提供代理账号")
                values["proxy_secret_id"] = self._secret_store.put_in_session(
                    session,
                    kind="SITE_PROXY_PASSWORD",
                    value=normalized_proxy_password.encode(),
                )
                connection_changed = True
            elif update_request.proxy_password_action == "CLEAR":
                values["proxy_secret_id"] = None
                connection_changed = True

            # A secondary download Cookie belongs to the original site/API
            # credential. Never carry it over to another kind or API key.
            old_download_secret_id = current.download_secret_id
            clear_download_secret = old_download_secret_id is not None and (
                next_kind is not current_kind
                or update_request.credential_action in {"SET", "CLEAR"}
                or update_request.download_cookie_action in {"SET", "CLEAR"}
            )
            if clear_download_secret:
                values["download_secret_id"] = None
                connection_changed = True
            if update_request.download_cookie_action == "SET":
                if normalized_download_cookie is None:
                    raise self._credential_invalid("下载 Cookie 不能为空")
                values["download_secret_id"] = self._secret_store.put_in_session(
                    session,
                    kind="SITE_DOWNLOAD_COOKIE",
                    value=normalized_download_cookie.encode("utf-8"),
                )
                connection_changed = True

            if connection_changed:
                values.update(
                    connection_status=SiteProbeStatus.UNTESTED.value,
                    capabilities={},
                    enabled=False,
                    risk_disabled_at=current.risk_disabled_at,
                )
            if not values:
                if current.version != expected_version:
                    raise self._version_conflict()
                return self._view(current)

            try:
                if not repository.update_config(
                    site_id,
                    expected_version=expected_version,
                    values=values,
                ):
                    session.rollback()
                    raise self._version_conflict()
                if old_secret_id is not None and update_request.credential_action in {
                    "SET",
                    "CLEAR",
                }:
                    self._secret_store.delete_in_session(session, old_secret_id)
                if old_proxy_secret_id is not None and update_request.proxy_password_action in {
                    "SET",
                    "CLEAR",
                }:
                    self._secret_store.delete_in_session(session, old_proxy_secret_id)
                if clear_download_secret and old_download_secret_id is not None:
                    self._secret_store.delete_in_session(session, old_download_secret_id)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise self._name_conflict() from exc
            view = self._view(self._require_record(repository, site_id))
            self._reliability_registry.discard(site_id)
            return view

    def delete(self, site_id: str, *, expected_version: int) -> None:
        with self._session_factory() as session:
            repository = SiteRepository(session)
            current = self._require_record(repository, site_id)
            if not repository.delete(site_id, expected_version=expected_version):
                session.rollback()
                raise self._version_conflict()
            if current.secret_id is not None:
                self._secret_store.delete_in_session(session, current.secret_id)
            if current.proxy_secret_id is not None:
                self._secret_store.delete_in_session(session, current.proxy_secret_id)
            if current.download_secret_id is not None:
                self._secret_store.delete_in_session(session, current.download_secret_id)
            session.commit()
        self._reliability_registry.discard(site_id)

    def set_enabled(self, site_id: str, *, expected_version: int, enabled: bool) -> SiteView:
        with self._session_factory() as session:
            repository = SiteRepository(session)
            current = self._require_record(repository, site_id)
            if enabled:
                # Defense in depth for imported/legacy rows that may carry a
                # future PENDING_ADAPTER type despite the public create/update gates.
                self._require_persistable_kind(SiteKind(current.type))
                if current.secret_id is None:
                    raise ApplicationError(
                        code="SITE_CREDENTIAL_REQUIRED",
                        status=409,
                        title="站点凭证未配置",
                        detail="启用站点前必须绑定对应类型的凭证",
                    )
                if current.type == SiteKind.ROUSI_PRO.value and current.download_secret_id is None:
                    raise ApplicationError(
                        code="SITE_DOWNLOAD_CREDENTIAL_REQUIRED",
                        status=409,
                        title="站点下载凭证未配置",
                        detail="Rousi Pro 启用前必须单独配置下载 Cookie",
                    )
            if current.enabled == enabled and (not enabled or current.risk_disabled_at is None):
                if current.version != expected_version:
                    raise self._version_conflict()
                return self._view(current)
            if not repository.update_config(
                site_id,
                expected_version=expected_version,
                values={
                    "enabled": enabled,
                    "risk_disabled_at": None if enabled else current.risk_disabled_at,
                },
            ):
                session.rollback()
                raise self._version_conflict()
            session.commit()
            view = self._view(self._require_record(repository, site_id))
            self._reliability_registry.discard(site_id)
            return view

    def record_unpack_risk_event(
        self,
        *,
        execution_id: str,
        item_id: str,
        site_config_id: str,
        error_code: str,
    ) -> SiteRiskControlOutcome | None:
        """Count anti-abuse responses by site and execution over a rolling 10-minute window."""
        if error_code not in SITE_RISK_CONTROL_ERROR_CODES:
            return None
        with self._session_factory() as session:
            begin_immediate_write(session)
            site = session.get(Site, site_config_id)
            if site is None:
                session.rollback()
                return None
            now = datetime.now(UTC)
            session.execute(
                delete(SiteRiskEvent).where(SiteRiskEvent.occurred_at < now - timedelta(days=30))
            )
            event = SiteRiskEvent(
                id=new_uuid(),
                site_id=site_config_id,
                execution_id=execution_id,
                item_id=item_id,
                error_code=error_code,
                occurred_at=now,
                threshold_reached=False,
            )
            session.add(event)
            session.flush()
            count = int(
                session.scalar(
                    select(func.count(SiteRiskEvent.id)).where(
                        SiteRiskEvent.site_id == site_config_id,
                        SiteRiskEvent.execution_id == execution_id,
                        SiteRiskEvent.occurred_at >= now - timedelta(minutes=10),
                    )
                )
                or 0
            )
            threshold_reached = count >= site.risk_control_limit
            if threshold_reached:
                event.threshold_reached = True
                if site.risk_disabled_at is None:
                    site.enabled = False
                    site.risk_disabled_at = now
                    site.version += 1
                    site.updated_at = now
            outcome = SiteRiskControlOutcome(
                risk_count=count,
                risk_limit=site.risk_control_limit,
                threshold_reached=threshold_reached,
                retry_interval_seconds=site.risk_retry_interval_seconds,
                risk_disabled_at=site.risk_disabled_at,
            )
            session.commit()
        if outcome.threshold_reached:
            self._reliability_registry.discard(site_config_id)
        return outcome

    async def test_connection(self, site_id: str) -> dict[str, object]:
        snapshot = self._connection_snapshot(site_id)
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_CREDENTIAL_REQUIRED",
                status=409,
                title="站点凭证未配置",
                detail="连接测试需要已配置的站点凭证",
            )
        credential = self._read_primary_credential(snapshot)
        adapter = self._reliability_registry.wrap(
            config_id=snapshot.id,
            config_version=snapshot.version,
            adapter=self._create_adapter(snapshot, credential),
            min_request_interval_seconds=snapshot.search_interval_seconds,
        )
        tested_at = datetime.now(UTC)
        try:
            result = await adapter.test_connection()
            capabilities = asdict(await adapter.capabilities())
        except SiteAdapterError as exc:
            self._store_connection_probe(
                snapshot,
                status=SiteProbeStatus.FAILED,
                capabilities={},
                tested_at=tested_at,
            )
            raise self._connection_error(exc) from exc
        expected_site_id = self._expected_site_id(snapshot.type)
        if result.site_id != expected_site_id:
            raise ApplicationError(
                code="SITE_IDENTITY_MISMATCH",
                status=502,
                title="站点连接身份不匹配",
                detail="站点适配器返回了意外的站点身份",
            )
        self._store_connection_probe(
            snapshot,
            status=SiteProbeStatus.OK,
            capabilities=capabilities,
            tested_at=tested_at,
        )
        return {"status": "ok", "capabilities": capabilities}

    def get_user_profile(self, site_id: str) -> SiteUserProfile | None:
        with self._session_factory() as session:
            record = session.get(SiteUserProfileSnapshot, site_id)
            if record is None:
                return None
            return SiteUserProfile(
                site_id=site_id,
                uid=record.uid,
                username=record.username,
                user_level=record.user_level,
                join_time=record.join_time,
                last_access_at=record.last_access_at,
                real_uploaded_bytes=record.real_uploaded_bytes,
                real_downloaded_bytes=record.real_downloaded_bytes,
                uploaded_bytes=record.uploaded_bytes,
                downloaded_bytes=record.downloaded_bytes,
                ratio=record.ratio,
                torrents_posted=record.torrents_posted,
                seeding_count=record.seeding_count,
                seeding_size_bytes=record.seeding_size_bytes,
                bonus=record.bonus,
                seeding_points=record.seeding_points,
                bonus_per_hour=record.bonus_per_hour,
                hnr_status=record.hnr_status,
                hnr_count=record.hnr_count,
                message_count=record.message_count,
                remark=record.remark,
                fetched_at=record.fetched_at,
            )

    def list_user_profile_sync_history(
        self, site_id: str, *, limit: int = 30
    ) -> list[dict[str, object]]:
        bounded_limit = min(max(limit, 1), 30)
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(SiteUserProfileSync)
                    .where(SiteUserProfileSync.site_id == site_id)
                    .order_by(SiteUserProfileSync.started_at.desc())
                    .limit(bounded_limit)
                )
            )
        return [
            {
                "id": row.id,
                "status": row.status,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "duration_ms": row.duration_ms,
                "field_count": row.field_count,
                "error_code": row.error_code,
                "error_message": row.error_message,
            }
            for row in rows
        ]

    async def sync_user_profile(self, site_id: str) -> SiteUserProfile:
        lock = self._profile_sync_locks.setdefault(site_id, asyncio.Lock())
        if lock.locked():
            raise ApplicationError(
                code="SITE_PROFILE_SYNC_IN_PROGRESS",
                status=409,
                title="站点用户信息正在同步",
                detail="同一站点同时只允许执行一个用户信息同步任务",
            )
        await lock.acquire()
        try:
            return await self._sync_user_profile_unlocked(site_id)
        finally:
            lock.release()

    async def _sync_user_profile_unlocked(self, site_id: str) -> SiteUserProfile:
        snapshot = self._connection_snapshot(site_id)
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_CREDENTIAL_REQUIRED",
                status=409,
                title="站点凭证未配置",
                detail="同步站点用户信息需要已配置的站点凭证",
            )

        started_at = datetime.now(UTC)
        started_clock = perf_counter()

        try:
            credential = self._read_primary_credential(snapshot)
            adapter = self._reliability_registry.wrap(
                config_id=snapshot.id,
                config_version=snapshot.version,
                adapter=self._create_adapter(snapshot, credential),
                min_request_interval_seconds=snapshot.search_interval_seconds,
            )
            profile = await adapter.fetch_user_profile()
            expected_site_id = self._expected_site_id(snapshot.type)
            if profile.site_id != expected_site_id:
                raise ApplicationError(
                    code="SITE_IDENTITY_MISMATCH",
                    status=502,
                    title="站点用户信息身份不匹配",
                    detail="站点适配器返回了意外的站点身份",
                )
        except SiteAdapterError as exc:
            finished_at = datetime.now(UTC)
            self._record_user_profile_sync(
                site_id,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=_duration_ms(started_clock),
                field_count=0,
                error_code=exc.code,
                # Adapter exception text can include untrusted remote response data.
                # Persist only the stable code in sync history, never raw exception text.
                error_message=f"站点用户信息同步失败（{exc.code}）",
            )
            raise self._profile_sync_error(exc) from exc
        except ApplicationError as exc:
            finished_at = datetime.now(UTC)
            self._record_user_profile_sync(
                site_id,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=_duration_ms(started_clock),
                field_count=0,
                error_code=exc.code,
                error_message=exc.detail,
            )
            raise
        except ValueError as exc:
            finished_at = datetime.now(UTC)
            self._record_user_profile_sync(
                site_id,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=_duration_ms(started_clock),
                field_count=0,
                error_code="SITE_INVALID_RESPONSE",
                # Parsing exceptions may echo the rejected remote value.
                error_message="站点返回的用户信息不符合 PackBreaker 数据约束",
            )
            raise ApplicationError(
                code="SITE_INVALID_RESPONSE",
                status=502,
                title="站点用户信息解析失败",
                detail="站点返回的用户信息不符合 PackBreaker 数据约束",
            ) from exc

        finished_at = datetime.now(UTC)
        field_count = _user_profile_field_count(profile)
        with self._session_factory() as session:
            record = session.get(SiteUserProfileSnapshot, site_id)
            values = _user_profile_record_values(profile)
            if record is None:
                session.add(SiteUserProfileSnapshot(site_id=site_id, **values))
            else:
                for field_name, value in values.items():
                    setattr(record, field_name, value)
                record.updated_at = finished_at
            session.add(
                SiteUserProfileSync(
                    site_id=site_id,
                    status="SUCCESS",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=_duration_ms(started_clock),
                    field_count=field_count,
                    error_code=None,
                    error_message=None,
                )
            )
            self._prune_user_profile_sync_history(session, site_id)
            session.commit()
        return profile

    def _record_user_profile_sync(
        self,
        site_id: str,
        *,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        field_count: int,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                SiteUserProfileSync(
                    site_id=site_id,
                    status=status,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    field_count=field_count,
                    error_code=error_code,
                    error_message=(error_message or "")[:512] or None,
                )
            )
            self._prune_user_profile_sync_history(session, site_id)
            session.commit()

    @staticmethod
    def _prune_user_profile_sync_history(session: Session, site_id: str) -> None:
        rows = list(
            session.scalars(
                select(SiteUserProfileSync)
                .where(SiteUserProfileSync.site_id == site_id)
                .order_by(SiteUserProfileSync.started_at.desc())
                .offset(30)
            )
        )
        for row in rows:
            session.delete(row)

    @staticmethod
    def _profile_sync_error(exc: SiteAdapterError) -> ApplicationError:
        return ApplicationError(
            code=exc.code,
            status=429 if exc.code == "SITE_RATE_LIMITED" else 502,
            title="站点用户信息同步失败",
            # Adapter errors can originate from untrusted remote responses; expose only
            # the stable error code rather than arbitrary exception text.
            detail=f"站点用户信息同步失败（{exc.code}）",
            retry_after=ceil(exc.retry_after_seconds)
            if exc.retry_after_seconds is not None
            else None,
        )

    async def probe(
        self,
        *,
        kind: SiteKind,
        credential_kind: SiteCredentialKind,
        credential: str,
        download_cookie: str | None = None,
        request_timeout_seconds: int = 15,
        search_interval_seconds: int = 0,
        user_agent: str | None = None,
        browser_emulation_enabled: bool = False,
        proxy_enabled: bool = False,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
    ) -> dict[str, object]:
        self._require_configurable_kind(kind)
        required_kind = required_site_credential_kind(kind)
        if credential_kind is not required_kind:
            raise self._credential_kind_mismatch(required_kind)
        normalized_credential = self._normalize_credential(required_kind, credential)
        normalized_download_cookie = self._validate_download_cookie(kind, download_cookie)
        runtime = self._normalize_runtime_config(
            kind,
            request_timeout_seconds=request_timeout_seconds,
            search_interval_seconds=search_interval_seconds,
            user_agent=user_agent,
            browser_emulation_enabled=browser_emulation_enabled,
            proxy_enabled=proxy_enabled,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_username=proxy_username,
        )
        normalized_proxy_password = self._normalize_proxy_password(proxy_password)
        if normalized_proxy_password is not None and not runtime["proxy_username"]:
            raise self._proxy_invalid("配置代理密码时必须同时提供代理账号")
        snapshot = _SiteConnectionSnapshot(
            id="temporary-probe",
            version=1,
            enabled=True,
            type=kind,
            base_url=trusted_site_base_url(kind),
            credential_kind=required_kind,
            secret_id=None,
            download_secret_id=None,
            request_timeout_seconds=runtime["request_timeout_seconds"],
            search_interval_seconds=runtime["search_interval_seconds"],
            user_agent=runtime["user_agent"],
            browser_emulation_enabled=runtime["browser_emulation_enabled"],
            proxy_enabled=runtime["proxy_enabled"],
            proxy_host=runtime["proxy_host"],
            proxy_port=runtime["proxy_port"],
            proxy_username=runtime["proxy_username"],
            proxy_secret_id=None,
        )
        adapter = self._create_adapter(
            snapshot,
            normalized_credential,
            proxy_password=normalized_proxy_password,
            download_cookie=normalized_download_cookie,
        )
        try:
            result = await adapter.test_connection()
            capabilities = asdict(await adapter.capabilities())
        except SiteAdapterError as exc:
            raise self._connection_error(exc) from exc
        if result.site_id != self._expected_site_id(kind):
            raise ApplicationError(
                code="SITE_IDENTITY_MISMATCH",
                status=502,
                title="站点连接身份不匹配",
                detail="站点适配器返回了意外的站点身份",
            )
        return {"status": "ok", "capabilities": capabilities}

    def _connection_snapshot(self, site_id: str) -> _SiteConnectionSnapshot:
        with self._session_factory() as session:
            current = self._require_record(SiteRepository(session), site_id)
            # Connection probes remain available independently from whether
            # the site is currently enabled for task execution.
            self._require_configurable_kind(SiteKind(current.type))
            return self._snapshot(current)

    @staticmethod
    def _snapshot(record: Site) -> _SiteConnectionSnapshot:
        return _SiteConnectionSnapshot(
            id=record.id,
            version=record.version,
            enabled=record.enabled,
            type=SiteKind(record.type),
            base_url=trusted_site_base_url(SiteKind(record.type)),
            credential_kind=SiteCredentialKind(record.credential_kind),
            secret_id=record.secret_id,
            download_secret_id=record.download_secret_id,
            request_timeout_seconds=record.request_timeout_seconds,
            search_interval_seconds=record.search_interval_seconds,
            user_agent=record.user_agent,
            browser_emulation_enabled=record.browser_emulation_enabled,
            proxy_enabled=record.proxy_enabled,
            proxy_host=record.proxy_host,
            proxy_port=record.proxy_port,
            proxy_username=record.proxy_username,
            proxy_secret_id=record.proxy_secret_id,
        )

    def _read_primary_credential(self, snapshot: _SiteConnectionSnapshot) -> str:
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_ENABLED_CONFIG_INVALID",
                status=409,
                title="站点主凭据引用无效",
                detail="站点主凭据未正确绑定",
            )
        try:
            return self._secret_store.get(
                snapshot.secret_id,
                expected_kind=self._secret_kind(required_site_credential_kind(snapshot.type)),
            ).decode("utf-8")
        except (SecretNotFound, SecretKindMismatch, UnicodeDecodeError):
            raise ApplicationError(
                code="SITE_ENABLED_CONFIG_INVALID",
                status=409,
                title="站点主凭据引用无效",
                detail="站点主凭据未指向有效的独立加密记录",
            ) from None

    def _create_adapter(
        self,
        snapshot: _SiteConnectionSnapshot,
        credential: str,
        *,
        proxy_password: str | None = None,
        download_cookie: str | None = None,
    ) -> SiteAdapter:
        if (
            snapshot.type is SiteKind.ROUSI_PRO
            and download_cookie is None
            and snapshot.download_secret_id is not None
        ):
            try:
                download_cookie = self._secret_store.get(
                    snapshot.download_secret_id, expected_kind="SITE_DOWNLOAD_COOKIE"
                ).decode("utf-8")
            except (SecretNotFound, SecretKindMismatch, UnicodeDecodeError):
                raise ApplicationError(
                    code="SITE_ENABLED_CONFIG_INVALID",
                    status=409,
                    title="站点下载凭据引用无效",
                    detail="Rousi Pro 下载凭据未指向有效的独立 Cookie 密文",
                ) from None
        return self._adapter_factory.create(
            kind=snapshot.type,
            base_url=snapshot.base_url,
            credential_kind=snapshot.credential_kind,
            credential=credential,
            download_cookie=download_cookie,
            timeout_seconds=snapshot.request_timeout_seconds,
            user_agent=snapshot.user_agent,
            browser_emulation_enabled=snapshot.browser_emulation_enabled,
            proxy_url=self._proxy_url(snapshot, proxy_password=proxy_password),
        )

    def _proxy_url(
        self,
        snapshot: _SiteConnectionSnapshot,
        *,
        proxy_password: str | None = None,
    ) -> str | None:
        if not snapshot.proxy_enabled:
            return None
        try:
            proxy = ProxyConfig(
                enabled=True,
                host=snapshot.proxy_host,
                port=snapshot.proxy_port,
                username=snapshot.proxy_username,
                credential_secret_id=snapshot.proxy_secret_id,
            )
        except ValueError as exc:
            raise self._proxy_invalid(str(exc)) from exc
        password = proxy_password
        if password is None and snapshot.proxy_secret_id is not None:
            try:
                password = self._secret_store.get(
                    snapshot.proxy_secret_id, expected_kind="SITE_PROXY_PASSWORD"
                ).decode("utf-8")
            except (SecretNotFound, SecretKindMismatch, UnicodeDecodeError):
                raise ApplicationError(
                    code="SITE_ENABLED_CONFIG_INVALID",
                    status=409,
                    title="站点代理凭据引用无效",
                    detail="站点代理密码未指向有效的独立加密记录",
                ) from None
        auth = ""
        if proxy.username is not None:
            auth = quote(proxy.username, safe="")
            if password is not None:
                auth = f"{auth}:{quote(password, safe='')}"
            auth = f"{auth}@"
        host = proxy.host or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{auth}{host}:{proxy.port}"

    def _store_connection_probe(
        self,
        snapshot: _SiteConnectionSnapshot,
        *,
        status: SiteProbeStatus,
        capabilities: dict[str, Any],
        tested_at: datetime,
    ) -> None:
        with self._session_factory() as session:
            if not SiteRepository(session).update_connection_probe(
                snapshot.id,
                expected_version=snapshot.version,
                status=status.value,
                capabilities=capabilities,
                tested_at=tested_at,
            ):
                session.rollback()
                raise self._version_conflict()
            session.commit()

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ApplicationError(
                code="SITE_NAME_INVALID",
                status=422,
                title="站点名称无效",
                detail="站点名称不能为空且最长 80 个字符",
            )
        return normalized

    @classmethod
    def _fixed_base_url(cls, kind: SiteKind, value: str | None) -> str:
        fixed = trusted_site_base_url(kind)
        if value is None:
            return fixed
        normalized = cls._normalize_url(kind, value)
        if normalized != fixed:
            raise ApplicationError(
                code="SITE_BASE_URL_FIXED",
                status=422,
                title="站点地址由类型固定",
                detail="站点地址由受信任 Profile 固定，不能自定义",
            )
        return fixed

    @staticmethod
    def _normalize_risk_control_config(
        *,
        risk_control_limit: object,
        risk_retry_interval_seconds: object,
    ) -> dict[str, int]:
        if (
            isinstance(risk_control_limit, bool)
            or not isinstance(risk_control_limit, int)
            or not 1 <= risk_control_limit <= 20
        ):
            raise ApplicationError(
                code="SITE_RISK_CONFIG_INVALID",
                status=422,
                title="站点风控配置无效",
                detail="风控次数限制必须是 1～20 的整数",
            )
        if (
            isinstance(risk_retry_interval_seconds, bool)
            or not isinstance(risk_retry_interval_seconds, int)
            or not 60 <= risk_retry_interval_seconds <= 86_400
            or risk_retry_interval_seconds % 60 != 0
        ):
            raise ApplicationError(
                code="SITE_RISK_CONFIG_INVALID",
                status=422,
                title="站点风控配置无效",
                detail="风控后再次执行间隔必须是 1～1440 分钟的整数",
            )
        return {
            "risk_control_limit": risk_control_limit,
            "risk_retry_interval_seconds": risk_retry_interval_seconds,
        }

    @classmethod
    def _normalize_runtime_config(
        cls,
        kind: SiteKind,
        *,
        request_timeout_seconds: object,
        search_interval_seconds: object,
        user_agent: object,
        browser_emulation_enabled: object,
        proxy_enabled: object,
        proxy_host: object,
        proxy_port: object,
        proxy_username: object,
    ) -> dict[str, Any]:
        profile = site_profile(kind)
        max_request_timeout_seconds = 3600 if kind is SiteKind.SPRING_SUNDAY else 120
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int)
            or not 1 <= request_timeout_seconds <= max_request_timeout_seconds
        ):
            raise cls._runtime_invalid(f"请求超时必须是 1～{max_request_timeout_seconds} 秒的整数")
        if (
            isinstance(search_interval_seconds, bool)
            or not isinstance(search_interval_seconds, int)
            or not 0 <= search_interval_seconds <= 3600
        ):
            raise cls._runtime_invalid("搜索间隔必须是 0～3600 秒的整数")
        if not isinstance(browser_emulation_enabled, bool) or not isinstance(proxy_enabled, bool):
            raise cls._runtime_invalid("浏览器仿真和代理开关必须是布尔值")

        normalized_ua: str | None = None
        if user_agent is not None:
            if not isinstance(user_agent, str):
                raise cls._runtime_invalid("User-Agent 格式无效")
            normalized_ua = user_agent.strip() or None
            if normalized_ua is not None and (
                len(normalized_ua) > 512
                or any(char in normalized_ua for char in ("\r", "\n", "\x00"))
            ):
                raise cls._runtime_invalid("User-Agent 格式无效")
        if not profile.supports_user_agent:
            if normalized_ua is not None:
                raise cls._runtime_invalid("该站点类型不支持自定义 User-Agent")
            normalized_ua = None
        elif normalized_ua is None:
            normalized_ua = DEFAULT_COOKIE_USER_AGENT
        if browser_emulation_enabled and not profile.supports_browser_emulation:
            raise cls._runtime_invalid("该站点类型不支持浏览器请求头仿真")
        if proxy_enabled and not profile.supports_proxy:
            raise cls._runtime_invalid("该站点类型不支持代理")

        try:
            proxy = ProxyConfig(
                enabled=proxy_enabled,
                host=proxy_host if isinstance(proxy_host, str) else None,
                port=(
                    proxy_port
                    if isinstance(proxy_port, int) and not isinstance(proxy_port, bool)
                    else None
                ),
                username=proxy_username if isinstance(proxy_username, str) else None,
            )
        except ValueError as exc:
            raise cls._proxy_invalid(str(exc)) from exc
        if proxy_host is not None and not isinstance(proxy_host, str):
            raise cls._proxy_invalid("代理地址格式无效")
        if proxy_port is not None and (
            isinstance(proxy_port, bool) or not isinstance(proxy_port, int)
        ):
            raise cls._proxy_invalid("代理端口格式无效")
        if proxy_username is not None and not isinstance(proxy_username, str):
            raise cls._proxy_invalid("代理账号格式无效")

        return {
            "request_timeout_seconds": request_timeout_seconds,
            "search_interval_seconds": search_interval_seconds,
            "user_agent": normalized_ua,
            "browser_emulation_enabled": browser_emulation_enabled,
            "proxy_enabled": proxy.enabled,
            "proxy_host": proxy.host,
            "proxy_port": proxy.port,
            "proxy_username": proxy.username,
        }

    @classmethod
    def _merged_runtime_config(
        cls,
        current: Site,
        kind: SiteKind,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "request_timeout_seconds",
            "search_interval_seconds",
            "user_agent",
            "browser_emulation_enabled",
            "proxy_enabled",
            "proxy_host",
            "proxy_port",
            "proxy_username",
        }
        unknown = set(patch) - allowed
        if unknown:
            raise cls._runtime_invalid("站点运行配置包含未知字段")
        current_kind = SiteKind(current.type)
        values: dict[str, Any] = {
            "request_timeout_seconds": current.request_timeout_seconds,
            "search_interval_seconds": current.search_interval_seconds,
            "user_agent": current.user_agent,
            "browser_emulation_enabled": current.browser_emulation_enabled,
            "proxy_enabled": current.proxy_enabled,
            "proxy_host": current.proxy_host,
            "proxy_port": current.proxy_port,
            "proxy_username": current.proxy_username,
        }
        if kind is not current_kind:
            profile = site_profile(kind)
            values.update(
                request_timeout_seconds=profile.request_timeout_seconds,
                search_interval_seconds=int(profile.search_interval_seconds),
                user_agent=(DEFAULT_COOKIE_USER_AGENT if profile.supports_user_agent else None),
                browser_emulation_enabled=False,
            )
        values.update(patch)
        return cls._normalize_runtime_config(kind, **values)

    @staticmethod
    def _normalize_proxy_password(value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.strip()
            or len(value) > 512
            or any(char in value for char in ("\r", "\n", "\x00"))
        ):
            raise SiteService._proxy_invalid("代理密码不能为空且最长 512 个字符")
        return value

    @staticmethod
    def _runtime_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="SITE_RUNTIME_CONFIG_INVALID",
            status=422,
            title="站点运行配置无效",
            detail=detail,
        )

    @staticmethod
    def _proxy_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="SITE_PROXY_INVALID",
            status=422,
            title="站点代理配置无效",
            detail=detail,
        )

    @staticmethod
    def _connection_error(exc: SiteAdapterError) -> ApplicationError:
        return ApplicationError(
            code=exc.code,
            status=429 if exc.code == "SITE_RATE_LIMITED" else 502,
            title="站点连接测试失败",
            detail=str(exc),
            retry_after=(
                ceil(exc.retry_after_seconds) if exc.retry_after_seconds is not None else None
            ),
        )

    @staticmethod
    def _normalize_url(kind: SiteKind, value: str) -> str:
        try:
            return normalize_site_base_url(kind, value)
        except ValueError as exc:
            raise ApplicationError(
                code="SITE_BASE_URL_INVALID",
                status=422,
                title="站点地址无效",
                detail=str(exc),
            ) from exc

    @classmethod
    def _normalize_credential(cls, kind: SiteCredentialKind, value: str) -> str:
        try:
            return normalize_site_credential(kind, value)
        except ValueError as exc:
            raise cls._credential_invalid(str(exc)) from exc

    @classmethod
    def _validate_download_cookie(cls, kind: SiteKind, value: str | None) -> str | None:
        if value is None:
            return None
        if kind is not SiteKind.ROUSI_PRO:
            raise ApplicationError(
                code="SITE_DOWNLOAD_CREDENTIAL_NOT_ALLOWED",
                status=422,
                title="该站点不支持独立下载凭证",
                detail="独立下载 Cookie 仅用于 Rousi Pro",
            )
        return cls._normalize_credential(SiteCredentialKind.COOKIE, value)

    @staticmethod
    def _credential_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="SITE_CREDENTIAL_INVALID",
            status=422,
            title="站点凭证无效",
            detail=detail,
        )

    @staticmethod
    def _credential_kind_mismatch(required: SiteCredentialKind) -> ApplicationError:
        return ApplicationError(
            code="SITE_CREDENTIAL_KIND_MISMATCH",
            status=422,
            title="站点凭证类型不匹配",
            detail=f"该站点类型要求使用 {required.value} 凭证",
        )

    @staticmethod
    def _secret_kind(kind: SiteCredentialKind) -> str:
        if kind is SiteCredentialKind.API_KEY:
            return "SITE_API_KEY"
        if kind is SiteCredentialKind.COOKIE:
            return "SITE_COOKIE"
        raise ValueError("未知站点凭证类型")

    @staticmethod
    def _require_configurable_kind(kind: SiteKind) -> None:
        if site_kind_is_configurable(kind):
            return
        profile = site_profile(kind)
        raise ApplicationError(
            code="SITE_ADAPTER_PENDING",
            status=409,
            title="站点尚未开放配置",
            detail=f"{profile.display_name} 当前不能配置或测试",
        )

    @staticmethod
    def _require_persistable_kind(kind: SiteKind) -> None:
        if site_kind_is_persistable(kind):
            return
        profile = site_profile(kind)
        raise ApplicationError(
            code="SITE_ADAPTER_PENDING",
            status=409,
            title="站点尚未开放正式任务",
            detail=f"{profile.display_name} 当前适配器尚未开放正式任务",
        )

    @staticmethod
    def _expected_site_id(kind: SiteKind) -> str:
        if kind is SiteKind.MTEAM:
            return "mteam"
        if kind is SiteKind.HDTIME:
            return "hdtime"
        if kind is SiteKind.HHCLUB:
            return "hhclub"
        if kind is SiteKind.ROUSI_PRO:
            return "rousi_pro"
        if kind in {
            SiteKind.KEEPFRDS,
            SiteKind.HDHOME,
            SiteKind.UBITS,
            SiteKind.HDFANS,
            SiteKind.BTSCHOOL,
            SiteKind.PTTIME,
            SiteKind.LINGYIN_CLUB,
            SiteKind.PTERCLUB,
            SiteKind.AUDIENCES,
            SiteKind.SPRING_SUNDAY,
            SiteKind.HDDOLBY,
            SiteKind.U2,
            SiteKind.TANGPT,
            SiteKind.CARPT,
        }:
            return kind.value.lower()
        raise ValueError("暂不支持该站点类型")

    @staticmethod
    def _name_conflict() -> ApplicationError:
        return ApplicationError(
            code="SITE_NAME_CONFLICT",
            status=409,
            title="站点名称冲突",
            detail="已存在同名站点配置",
        )

    @staticmethod
    def _version_conflict() -> ApplicationError:
        return ApplicationError(
            code="SITE_VERSION_CONFLICT",
            status=412,
            title="站点配置版本冲突",
            detail="站点配置已变化，请刷新后重试",
        )

    @staticmethod
    def _require_record(repository: SiteRepository, site_id: str) -> Site:
        record = repository.get(site_id)
        if record is None:
            raise ApplicationError(
                code="SITE_NOT_FOUND",
                status=404,
                title="站点不存在",
                detail="未找到指定站点配置",
            )
        return record

    @staticmethod
    def _view(record: Site) -> SiteView:
        return SiteView(
            id=record.id,
            name=record.name,
            type=SiteKind(record.type),
            base_url=trusted_site_base_url(SiteKind(record.type)),
            credential_kind=SiteCredentialKind(record.credential_kind),
            credential_configured=record.secret_id is not None,
            download_credential_configured=record.download_secret_id is not None,
            request_timeout_seconds=record.request_timeout_seconds,
            search_interval_seconds=record.search_interval_seconds,
            risk_control_limit=record.risk_control_limit,
            risk_retry_interval_seconds=record.risk_retry_interval_seconds,
            risk_disabled_at=record.risk_disabled_at,
            user_agent=record.user_agent,
            browser_emulation_enabled=record.browser_emulation_enabled,
            proxy_enabled=record.proxy_enabled,
            proxy_host=record.proxy_host,
            proxy_port=record.proxy_port,
            proxy_username=record.proxy_username,
            proxy_credential_configured=record.proxy_secret_id is not None,
            capabilities=dict(record.capabilities),
            support_status=site_profile(SiteKind(record.type)).support_status,
            connection_status=SiteProbeStatus(record.connection_status),
            enabled=record.enabled,
            version=record.version,
            last_test_at=record.last_test_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
