from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any, Literal
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretStore
from backend.app.domain.proxy import ProxyConfig
from backend.app.domain.site_adapter import SiteAdapter, SiteUserProfile
from backend.app.domain.site_config import (
    DEFAULT_COOKIE_USER_AGENT,
    SiteCredentialKind,
    SiteKind,
    SiteProbeStatus,
    normalize_site_base_url,
    normalize_site_credential,
    required_site_credential_kind,
    site_kind_is_persistable,
    site_profile,
    trusted_site_base_url,
)
from backend.app.infrastructure.adapters.sites import SiteAdapterError, SiteAdapterFactory
from backend.app.infrastructure.persistence.models import Site
from backend.app.infrastructure.persistence.site_repositories import SiteRepository
from backend.app.infrastructure.site_reliability import (
    SiteReliabilityHealth,
    SiteReliabilityRegistry,
)


@dataclass(frozen=True, slots=True)
class SiteView:
    id: str
    name: str
    type: SiteKind
    base_url: str
    credential_kind: SiteCredentialKind
    credential_configured: bool
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
    connection_status: SiteProbeStatus
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SiteUpdate:
    name: str | None = None
    type: SiteKind | None = None
    base_url: str | None = None
    credential_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    credential_kind: SiteCredentialKind | None = None
    credential: str | None = None
    runtime_config: dict[str, Any] | None = None
    proxy_password_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    proxy_password: str | None = None


@dataclass(frozen=True, slots=True)
class _SiteConnectionSnapshot:
    id: str
    version: int
    type: SiteKind
    base_url: str
    credential_kind: SiteCredentialKind
    secret_id: str | None
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


class SiteService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        adapter_factory: SiteAdapterFactory | None = None,
        reliability_registry: SiteReliabilityRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._adapter_factory = adapter_factory or SiteAdapterFactory()
        self._reliability_registry = reliability_registry or SiteReliabilityRegistry()

    def list_sites(self) -> list[SiteView]:
        with self._session_factory() as session:
            return [self._view(record) for record in SiteRepository(session).list_all()]

    def get(self, site_id: str) -> SiteView:
        with self._session_factory() as session:
            return self._view(self._require_record(SiteRepository(session), site_id))

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
        result: list[EnabledSiteAdapter] = []
        for snapshot in snapshots:
            if snapshot.secret_id is None:
                raise ApplicationError(
                    code="SITE_ENABLED_CONFIG_INVALID",
                    status=500,
                    title="已启用站点配置无效",
                    detail="已启用站点缺少凭证",
                )
            credential = self._secret_store.get(snapshot.secret_id).decode("utf-8")
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
                )
            )
        return tuple(result)

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        with self._session_factory() as session:
            return tuple(
                (record.id, record.version) for record in SiteRepository(session).list_enabled()
            )

    def create(
        self,
        *,
        name: str,
        kind: SiteKind,
        base_url: str | None = None,
        credential_kind: SiteCredentialKind | None,
        credential: str | None,
        request_timeout_seconds: int = 15,
        search_interval_seconds: int = 0,
        user_agent: str | None = None,
        browser_emulation_enabled: bool = False,
        proxy_enabled: bool = False,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
    ) -> SiteView:
        self._require_persistable_kind(kind)
        normalized_name = self._normalize_name(name)
        normalized_url = self._fixed_base_url(kind, base_url)
        required_kind = required_site_credential_kind(kind)
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
                    request_timeout_seconds=runtime["request_timeout_seconds"],
                    search_interval_seconds=runtime["search_interval_seconds"],
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
            self._require_persistable_kind(next_kind)
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

            if connection_changed:
                values.update(
                    connection_status=SiteProbeStatus.UNTESTED.value,
                    capabilities={},
                    enabled=False,
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
            session.commit()
        self._reliability_registry.discard(site_id)

    def set_enabled(self, site_id: str, *, expected_version: int, enabled: bool) -> SiteView:
        with self._session_factory() as session:
            repository = SiteRepository(session)
            current = self._require_record(repository, site_id)
            if enabled:
                if current.secret_id is None:
                    raise ApplicationError(
                        code="SITE_CREDENTIAL_REQUIRED",
                        status=409,
                        title="站点凭证未配置",
                        detail="启用站点前必须绑定对应类型的凭证",
                    )
                if current.connection_status != SiteProbeStatus.OK.value:
                    raise ApplicationError(
                        code="SITE_CONNECTION_TEST_REQUIRED",
                        status=409,
                        title="站点连接尚未验证",
                        detail="启用站点前必须通过只读连接测试",
                    )
            if current.enabled == enabled:
                if current.version != expected_version:
                    raise self._version_conflict()
                return self._view(current)
            if not repository.update_config(
                site_id,
                expected_version=expected_version,
                values={"enabled": enabled},
            ):
                session.rollback()
                raise self._version_conflict()
            session.commit()
            view = self._view(self._require_record(repository, site_id))
            self._reliability_registry.discard(site_id)
            return view

    async def test_connection(self, site_id: str) -> dict[str, object]:
        snapshot = self._connection_snapshot(site_id)
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_CREDENTIAL_REQUIRED",
                status=409,
                title="站点凭证未配置",
                detail="连接测试需要已配置的站点凭证",
            )
        credential = self._secret_store.get(snapshot.secret_id).decode("utf-8")
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

    async def probe(
        self,
        *,
        kind: SiteKind,
        credential_kind: SiteCredentialKind,
        credential: str,
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
        self._require_persistable_kind(kind)
        required_kind = required_site_credential_kind(kind)
        if credential_kind is not required_kind:
            raise self._credential_kind_mismatch(required_kind)
        normalized_credential = self._normalize_credential(required_kind, credential)
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
            type=kind,
            base_url=trusted_site_base_url(kind),
            credential_kind=required_kind,
            secret_id=None,
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

    async def user_profile(self, site_id: str) -> SiteUserProfile:
        snapshot = self._connection_snapshot(site_id)
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_CREDENTIAL_REQUIRED",
                status=409,
                title="站点凭证未配置",
                detail="读取站点用户详情需要已配置的站点凭证",
            )
        credential = self._secret_store.get(snapshot.secret_id).decode("utf-8")
        adapter = self._reliability_registry.wrap(
            config_id=snapshot.id,
            config_version=snapshot.version,
            adapter=self._create_adapter(snapshot, credential),
            min_request_interval_seconds=snapshot.search_interval_seconds,
        )
        try:
            profile = await adapter.fetch_user_profile()
        except SiteAdapterError as exc:
            raise ApplicationError(
                code=exc.code,
                status=429 if exc.code == "SITE_RATE_LIMITED" else 502,
                title="站点用户详情获取失败",
                detail=str(exc),
                retry_after=(
                    ceil(exc.retry_after_seconds) if exc.retry_after_seconds is not None else None
                ),
            ) from exc
        if profile.site_id != self._expected_site_id(snapshot.type):
            raise ApplicationError(
                code="SITE_IDENTITY_MISMATCH",
                status=502,
                title="站点用户详情身份不匹配",
                detail="站点适配器返回了意外的站点身份",
            )
        # A remote read may finish after another client replaces the site's
        # credentials, switches its type, or deletes it. Never return personal
        # statistics captured under an obsolete connection snapshot.
        with self._session_factory() as session:
            current = self._require_record(SiteRepository(session), site_id)
            if current.version != snapshot.version or current.secret_id != snapshot.secret_id:
                raise self._version_conflict()
        return profile

    def _connection_snapshot(self, site_id: str) -> _SiteConnectionSnapshot:
        with self._session_factory() as session:
            current = self._require_record(SiteRepository(session), site_id)
            return self._snapshot(current)

    @staticmethod
    def _snapshot(record: Site) -> _SiteConnectionSnapshot:
        return _SiteConnectionSnapshot(
            id=record.id,
            version=record.version,
            type=SiteKind(record.type),
            base_url=trusted_site_base_url(SiteKind(record.type)),
            credential_kind=SiteCredentialKind(record.credential_kind),
            secret_id=record.secret_id,
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

    def _create_adapter(
        self,
        snapshot: _SiteConnectionSnapshot,
        credential: str,
        *,
        proxy_password: str | None = None,
    ) -> SiteAdapter:
        return self._adapter_factory.create(
            kind=snapshot.type,
            base_url=snapshot.base_url,
            credential_kind=snapshot.credential_kind,
            credential=credential,
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
            password = self._secret_store.get(snapshot.proxy_secret_id).decode("utf-8")
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
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int)
            or not 1 <= request_timeout_seconds <= 120
        ):
            raise cls._runtime_invalid("请求超时必须是 1～120 秒的整数")
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
    def _require_persistable_kind(kind: SiteKind) -> None:
        if site_kind_is_persistable(kind):
            return
        profile = site_profile(kind)
        raise ApplicationError(
            code="SITE_ADAPTER_PENDING",
            status=409,
            title="站点适配尚未完成",
            detail=(
                f"{profile.display_name} 已进入 Profile Registry，但当前版本尚未开放配置和连接测试"
            ),
        )

    @staticmethod
    def _expected_site_id(kind: SiteKind) -> str:
        if kind is SiteKind.MTEAM:
            return "mteam"
        if kind is SiteKind.HDTIME:
            return "hdtime"
        if kind is SiteKind.HHCLUB:
            return "hhclub"
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
            request_timeout_seconds=record.request_timeout_seconds,
            search_interval_seconds=record.search_interval_seconds,
            user_agent=record.user_agent,
            browser_emulation_enabled=record.browser_emulation_enabled,
            proxy_enabled=record.proxy_enabled,
            proxy_host=record.proxy_host,
            proxy_port=record.proxy_port,
            proxy_username=record.proxy_username,
            proxy_credential_configured=record.proxy_secret_id is not None,
            capabilities=dict(record.capabilities),
            connection_status=SiteProbeStatus(record.connection_status),
            enabled=record.enabled,
            version=record.version,
            last_test_at=record.last_test_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
