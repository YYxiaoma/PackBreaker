from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretStore
from backend.app.domain.site_config import SiteKind, SiteProbeStatus, normalize_site_base_url
from backend.app.infrastructure.adapters.sites import SiteAdapterError, SiteAdapterFactory
from backend.app.infrastructure.persistence.models import Site
from backend.app.infrastructure.persistence.site_repositories import SiteRepository

_API_KEY_SECRET_KIND = "SITE_API_KEY"


@dataclass(frozen=True, slots=True)
class SiteView:
    id: str
    name: str
    type: SiteKind
    base_url: str
    api_key_configured: bool
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
    api_key_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class _SiteConnectionSnapshot:
    id: str
    version: int
    type: SiteKind
    base_url: str
    secret_id: str | None


class SiteService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        adapter_factory: SiteAdapterFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._adapter_factory = adapter_factory or SiteAdapterFactory()

    def list_sites(self) -> list[SiteView]:
        with self._session_factory() as session:
            return [self._view(record) for record in SiteRepository(session).list_all()]

    def get(self, site_id: str) -> SiteView:
        with self._session_factory() as session:
            return self._view(self._require_record(SiteRepository(session), site_id))

    def create(
        self,
        *,
        name: str,
        kind: SiteKind,
        base_url: str,
        api_key: str | None,
    ) -> SiteView:
        normalized_name = self._normalize_name(name)
        normalized_url = self._normalize_url(kind, base_url)
        normalized_key = self._normalize_api_key(api_key) if api_key is not None else None
        with self._session_factory() as session:
            secret_id = (
                self._secret_store.put_in_session(
                    session,
                    kind=_API_KEY_SECRET_KIND,
                    value=normalized_key.encode(),
                )
                if normalized_key is not None
                else None
            )
            try:
                record = SiteRepository(session).create(
                    name=normalized_name,
                    kind=kind.value,
                    base_url=normalized_url,
                    secret_id=secret_id,
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
            if (
                update_request.type is not None
                and update_request.type is not current_kind
                and current.secret_id is not None
                and update_request.api_key_action == "KEEP"
            ):
                raise ApplicationError(
                    code="SITE_API_KEY_REPLACEMENT_REQUIRED",
                    status=422,
                    title="站点 API Key 需要更新",
                    detail="切换站点类型时必须同时替换或清除现有 API Key",
                )

            values: dict[str, Any] = {}
            connection_changed = (
                update_request.type is not None or update_request.base_url is not None
            )
            if update_request.name is not None:
                values["name"] = self._normalize_name(update_request.name)
            if update_request.type is not None:
                values["type"] = next_kind.value
            if update_request.base_url is not None:
                values["base_url"] = self._normalize_url(next_kind, update_request.base_url)

            old_secret_id = current.secret_id
            if update_request.api_key_action == "SET":
                if update_request.api_key is None:
                    raise self._api_key_invalid("新 API Key 不能为空")
                normalized_key = self._normalize_api_key(update_request.api_key)
                values["secret_id"] = self._secret_store.put_in_session(
                    session,
                    kind=_API_KEY_SECRET_KIND,
                    value=normalized_key.encode(),
                )
                connection_changed = True
            elif update_request.api_key_action == "CLEAR":
                values["secret_id"] = None
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
                if old_secret_id is not None and update_request.api_key_action in {"SET", "CLEAR"}:
                    self._secret_store.delete_in_session(session, old_secret_id)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise self._name_conflict() from exc
            return self._view(self._require_record(repository, site_id))

    def delete(self, site_id: str, *, expected_version: int) -> None:
        with self._session_factory() as session:
            repository = SiteRepository(session)
            current = self._require_record(repository, site_id)
            if not repository.delete(site_id, expected_version=expected_version):
                session.rollback()
                raise self._version_conflict()
            if current.secret_id is not None:
                self._secret_store.delete_in_session(session, current.secret_id)
            session.commit()

    def set_enabled(self, site_id: str, *, expected_version: int, enabled: bool) -> SiteView:
        with self._session_factory() as session:
            repository = SiteRepository(session)
            current = self._require_record(repository, site_id)
            if enabled:
                if current.secret_id is None:
                    raise ApplicationError(
                        code="SITE_API_KEY_REQUIRED",
                        status=409,
                        title="站点 API Key 未配置",
                        detail="启用站点前必须绑定 API Key",
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
            return self._view(self._require_record(repository, site_id))

    async def test_connection(self, site_id: str) -> dict[str, object]:
        snapshot = self._connection_snapshot(site_id)
        if snapshot.secret_id is None:
            raise ApplicationError(
                code="SITE_API_KEY_REQUIRED",
                status=409,
                title="站点 API Key 未配置",
                detail="连接测试需要已配置的站点 API Key",
            )
        api_key = self._secret_store.get(snapshot.secret_id).decode("utf-8")
        adapter = self._adapter_factory.create(
            kind=snapshot.type,
            base_url=snapshot.base_url,
            api_key=api_key,
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
            raise ApplicationError(
                code=exc.code,
                status=429 if exc.code == "SITE_RATE_LIMITED" else 502,
                title="站点连接测试失败",
                detail=str(exc),
                retry_after=(
                    ceil(exc.retry_after_seconds) if exc.retry_after_seconds is not None else None
                ),
            ) from exc
        expected_site_id = "mteam" if snapshot.type is SiteKind.MTEAM else ""
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

    def _connection_snapshot(self, site_id: str) -> _SiteConnectionSnapshot:
        with self._session_factory() as session:
            current = self._require_record(SiteRepository(session), site_id)
            return _SiteConnectionSnapshot(
                current.id,
                current.version,
                SiteKind(current.type),
                current.base_url,
                current.secret_id,
            )

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

    @staticmethod
    def _normalize_url(kind: SiteKind, value: str) -> str:
        try:
            return normalize_site_base_url(kind, value)
        except ValueError as exc:
            raise ApplicationError(
                code="SITE_BASE_URL_INVALID",
                status=422,
                title="站点 API 地址无效",
                detail=str(exc),
            ) from exc

    @classmethod
    def _normalize_api_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 512:
            raise cls._api_key_invalid("API Key 不能为空且最长 512 个字符")
        return normalized

    @staticmethod
    def _api_key_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="SITE_API_KEY_INVALID",
            status=422,
            title="站点 API Key 无效",
            detail=detail,
        )

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
            base_url=record.base_url,
            api_key_configured=record.secret_id is not None,
            capabilities=dict(record.capabilities),
            connection_status=SiteProbeStatus(record.connection_status),
            enabled=record.enabled,
            version=record.version,
            last_test_at=record.last_test_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
