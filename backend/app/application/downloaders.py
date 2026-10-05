import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretStore
from backend.app.domain.downloader import (
    DownloaderCredential,
    DownloaderRuntimeMetrics,
    PathMappingRule,
    ProbeStatus,
    downloader_execution_binding_digest,
    map_remote_path,
    normalize_base_url,
    normalize_path_mappings,
    reverse_map_container_path_unique,
    validate_credential,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    DownloaderAdapterFactory,
    DownloaderTorrent,
    DownloaderTorrentListAdapter,
    QbittorrentWriteAdapter,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.persistence.downloader_repositories import DownloaderRepository
from backend.app.infrastructure.persistence.models import Downloader, UnpackTask

_CREDENTIAL_KIND = "DOWNLOADER_CREDENTIAL"


@dataclass(frozen=True, slots=True)
class DownloaderView:
    id: str
    name: str
    type: DownloaderKind
    base_url: str
    credential_configured: bool
    monitor_rules: dict[str, Any]
    path_mappings: tuple[PathMappingRule, ...]
    capabilities: dict[str, Any]
    connection_status: ProbeStatus
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DownloaderTorrentListView:
    items: tuple[DownloaderTorrent, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class DownloaderUpdate:
    name: str | None = None
    type: DownloaderKind | None = None
    base_url: str | None = None
    monitor_rules: dict[str, Any] | None = None
    path_mappings: list[PathMappingRule] | None = None
    credential_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    credential: DownloaderCredential | None = None


@dataclass(frozen=True, slots=True)
class QbittorrentWriteBinding:
    downloader_id: str
    downloader_version: int
    binding_digest: str
    path_mappings: tuple[PathMappingRule, ...]
    capabilities: dict[str, Any]
    adapter: QbittorrentWriteAdapter
    data_root: Path

    def remote_save_path(self, container_path: Path) -> str:
        return reverse_map_container_path_unique(
            container_path,
            list(self.path_mappings),
        )

    def container_path(self, remote_path: str) -> Path:
        return map_remote_path(
            remote_path,
            list(self.path_mappings),
        ).container_path


@dataclass(frozen=True, slots=True)
class TransmissionWriteBinding:
    downloader_id: str
    downloader_version: int
    binding_digest: str
    path_mappings: tuple[PathMappingRule, ...]
    capabilities: dict[str, Any]
    adapter: TransmissionWriteAdapter
    data_root: Path

    def remote_save_path(self, container_path: Path) -> str:
        return reverse_map_container_path_unique(
            container_path,
            list(self.path_mappings),
        )

    def container_path(self, remote_path: str) -> Path:
        return map_remote_path(
            remote_path,
            list(self.path_mappings),
        ).container_path


class DownloaderService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        data_root: Path,
        adapter_factory: DownloaderAdapterFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._data_root = data_root.resolve(strict=False)
        self._adapter_factory = adapter_factory or DownloaderAdapterFactory()

    def list_downloaders(self) -> list[DownloaderView]:
        with self._session_factory() as session:
            return [self._view(record) for record in DownloaderRepository(session).list_all()]

    def get(self, downloader_id: str) -> DownloaderView:
        with self._session_factory() as session:
            return self._view(self._require_record(DownloaderRepository(session), downloader_id))

    async def list_torrents(
        self,
        downloader_id: str,
        *,
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        status: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        tracker: str | None = None,
        save_path: str | None = None,
    ) -> DownloaderTorrentListView:
        if page < 1 or page_size < 1 or page_size > 200:
            raise ApplicationError(
                code="DOWNLOADER_TORRENT_PAGE_INVALID",
                status=422,
                title="种子分页参数无效",
                detail="page 必须大于等于 1，page_size 必须位于 1..200",
            )
        items = await self.list_all_torrents(downloader_id)
        needles = {
            "search": (search or "").strip().casefold(),
            "status": (status or "").strip().casefold(),
            "category": (category or "").strip().casefold(),
            "tag": (tag or "").strip().casefold(),
            "tracker": (tracker or "").strip().casefold(),
            "save_path": (save_path or "").strip().casefold(),
        }

        def matches(item: DownloaderTorrent) -> bool:
            if needles["search"] and needles["search"] not in item.name.casefold():
                return False
            if needles["status"] and needles["status"] not in item.status.casefold():
                return False
            if needles["category"] and needles["category"] not in (item.category or "").casefold():
                return False
            if needles["tag"] and all(
                needles["tag"] not in value.casefold() for value in item.tags
            ):
                return False
            if needles["tracker"] and needles["tracker"] not in (item.tracker or "").casefold():
                return False
            return not needles["save_path"] or needles["save_path"] in item.save_path.casefold()

        filtered = tuple(
            sorted(
                (item for item in items if matches(item)),
                key=lambda item: (item.name.casefold(), item.torrent_hash),
            )
        )
        offset = (page - 1) * page_size
        return DownloaderTorrentListView(
            items=filtered[offset : offset + page_size],
            page=page,
            page_size=page_size,
            total=len(filtered),
        )

    async def list_all_torrents(self, downloader_id: str) -> tuple[DownloaderTorrent, ...]:
        binding = self.write_binding(downloader_id)
        adapter = cast(DownloaderTorrentListAdapter, binding.adapter)
        try:
            return await adapter.list_torrents()
        except DownloaderAdapterError as exc:
            raise ApplicationError(
                code=exc.code,
                status=502,
                title="读取下载器种子失败",
                detail=str(exc),
            ) from exc

    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        with self._session_factory() as session:
            record = self._require_record(DownloaderRepository(session), downloader_id)
            kind = DownloaderKind(record.type)
        if kind is DownloaderKind.QBITTORRENT:
            return self.qbittorrent_write_binding(downloader_id)
        if kind is DownloaderKind.TRANSMISSION:
            return self.transmission_write_binding(downloader_id)
        raise ApplicationError(
            code="DOWNLOADER_KIND_UNSUPPORTED",
            status=409,
            title="目标下载器类型不支持",
            detail="当前写入主链不支持该下载器类型",
        )

    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
        with self._session_factory() as session:
            record = self._require_record(DownloaderRepository(session), downloader_id)
            if DownloaderKind(record.type) is not DownloaderKind.QBITTORRENT:
                raise ApplicationError(
                    code="DOWNLOADER_KIND_UNSUPPORTED",
                    status=409,
                    title="目标下载器类型不支持",
                    detail="当前写入切片只支持 qBittorrent",
                )
            if not record.enabled:
                raise ApplicationError(
                    code="DOWNLOADER_NOT_ENABLED",
                    status=409,
                    title="目标下载器未启用",
                    detail="下载器写操作只能使用已通过安全门并启用的配置",
                )
            if record.connection_status != ProbeStatus.OK.value:
                raise ApplicationError(
                    code="DOWNLOADER_NOT_READY",
                    status=409,
                    title="目标下载器安全门未就绪",
                    detail="连接探测必须保持 OK；路径映射在任务执行时按实际路径安全校验",
                )
            if record.secret_id is None:
                raise ApplicationError(
                    code="DOWNLOADER_CREDENTIAL_REQUIRED",
                    status=409,
                    title="目标下载器凭证未配置",
                    detail="qBittorrent 写操作需要已配置凭证",
                )
            api_version = record.capabilities.get("api_version")
            if not isinstance(api_version, str) or not api_version:
                raise ApplicationError(
                    code="DOWNLOADER_CAPABILITIES_STALE",
                    status=409,
                    title="目标下载器能力信息不可用",
                    detail="执行写操作前需要重新探测 qBittorrent WebAPI 能力",
                )
            snapshot_id = record.id
            snapshot_version = record.version
            secret_id = record.secret_id
            mappings = tuple(self._record_mappings(record))
            capabilities = dict(record.capabilities)
            base_url = record.base_url

        credential = self._decode_credential(self._secret_store.get(secret_id))
        return QbittorrentWriteBinding(
            downloader_id=snapshot_id,
            downloader_version=snapshot_version,
            binding_digest=downloader_execution_binding_digest(
                downloader_id=snapshot_id,
                version=snapshot_version,
                kind=DownloaderKind.QBITTORRENT,
                enabled=True,
                connection_status=ProbeStatus.OK,
                path_mappings=mappings,
                capabilities=capabilities,
            ),
            path_mappings=mappings,
            capabilities=capabilities,
            adapter=self._adapter_factory.create_qbittorrent(
                base_url=base_url,
                credential=credential,
            ),
            data_root=self._data_root,
        )

    def transmission_write_binding(self, downloader_id: str) -> TransmissionWriteBinding:
        with self._session_factory() as session:
            record = self._require_record(DownloaderRepository(session), downloader_id)
            if DownloaderKind(record.type) is not DownloaderKind.TRANSMISSION:
                raise ApplicationError(
                    code="DOWNLOADER_KIND_UNSUPPORTED",
                    status=409,
                    title="目标下载器类型不支持",
                    detail="当前 Transmission 写入 binding 只能使用 Transmission 配置",
                )
            if not record.enabled:
                raise ApplicationError(
                    code="DOWNLOADER_NOT_ENABLED",
                    status=409,
                    title="目标下载器未启用",
                    detail="下载器写操作只能使用已通过安全门并启用的配置",
                )
            if record.connection_status != ProbeStatus.OK.value:
                raise ApplicationError(
                    code="DOWNLOADER_NOT_READY",
                    status=409,
                    title="目标下载器安全门未就绪",
                    detail="连接探测必须保持 OK；路径映射在任务执行时按实际路径安全校验",
                )
            api_version = record.capabilities.get("api_version")
            if not isinstance(api_version, str) or not api_version:
                raise ApplicationError(
                    code="DOWNLOADER_CAPABILITIES_STALE",
                    status=409,
                    title="目标下载器能力信息不可用",
                    detail="执行写操作前需要重新探测 Transmission RPC 能力",
                )
            snapshot_id = record.id
            snapshot_version = record.version
            secret_id = record.secret_id
            mappings = tuple(self._record_mappings(record))
            capabilities = dict(record.capabilities)
            base_url = record.base_url

        credential = (
            self._decode_credential(self._secret_store.get(secret_id))
            if secret_id is not None
            else None
        )
        return TransmissionWriteBinding(
            downloader_id=snapshot_id,
            downloader_version=snapshot_version,
            binding_digest=downloader_execution_binding_digest(
                downloader_id=snapshot_id,
                version=snapshot_version,
                kind=DownloaderKind.TRANSMISSION,
                enabled=True,
                connection_status=ProbeStatus.OK,
                path_mappings=mappings,
                capabilities=capabilities,
            ),
            path_mappings=mappings,
            capabilities=capabilities,
            adapter=self._adapter_factory.create_transmission(
                base_url=base_url,
                credential=credential,
            ),
            data_root=self._data_root,
        )

    def create(
        self,
        *,
        name: str,
        kind: DownloaderKind,
        base_url: str,
        credential: DownloaderCredential | None,
        monitor_rules: dict[str, Any],
        path_mappings: list[PathMappingRule],
    ) -> DownloaderView:
        normalized_name = self._normalize_name(name)
        normalized_url = self._normalize_url(kind, base_url)
        normalized_mappings = self._normalize_mappings(path_mappings)
        self._validate_credential(kind, credential)
        with self._session_factory() as session:
            secret_id = (
                self._secret_store.put_in_session(
                    session,
                    kind=_CREDENTIAL_KIND,
                    value=self._encode_credential(credential),
                )
                if credential is not None
                else None
            )
            try:
                record = DownloaderRepository(session).create(
                    name=normalized_name,
                    kind=kind.value,
                    base_url=normalized_url,
                    secret_id=secret_id,
                    monitor_rules=monitor_rules,
                    path_mappings=self._mapping_json(normalized_mappings),
                )
                session.commit()
                session.refresh(record)
            except IntegrityError as exc:
                session.rollback()
                raise self._name_conflict() from exc
            return self._view(record)

    def update(
        self,
        downloader_id: str,
        *,
        expected_version: int,
        update_request: DownloaderUpdate,
    ) -> DownloaderView:
        with self._session_factory() as session:
            repository = DownloaderRepository(session)
            current = self._require_record(repository, downloader_id)
            current_kind = DownloaderKind(current.type)
            next_kind = update_request.type or current_kind
            if (
                update_request.type is not None
                and update_request.type is not current_kind
                and current.secret_id is not None
                and update_request.credential_action == "KEEP"
            ):
                raise ApplicationError(
                    code="DOWNLOADER_CREDENTIAL_REPLACEMENT_REQUIRED",
                    status=422,
                    title="下载器凭证需要更新",
                    detail="切换下载器类型时必须同时替换或清除现有凭证",
                )

            values: dict[str, Any] = {}
            safety_connection_changed = (
                update_request.type is not None or update_request.base_url is not None
            )
            if update_request.name is not None:
                values["name"] = self._normalize_name(update_request.name)
            if update_request.type is not None:
                values["type"] = next_kind.value
            if update_request.base_url is not None:
                values["base_url"] = self._normalize_url(next_kind, update_request.base_url)
            if update_request.monitor_rules is not None:
                values["monitor_rules"] = dict(update_request.monitor_rules)
            if update_request.path_mappings is not None:
                mappings = self._normalize_mappings(update_request.path_mappings)
                values["path_mappings"] = self._mapping_json(mappings)

            old_secret_id = current.secret_id
            if update_request.credential_action == "SET":
                if update_request.credential is None:
                    raise self._credential_invalid("新凭证不能为空")
                self._validate_credential(next_kind, update_request.credential)
                values["secret_id"] = self._secret_store.put_in_session(
                    session,
                    kind=_CREDENTIAL_KIND,
                    value=self._encode_credential(update_request.credential),
                )
                safety_connection_changed = True
            elif update_request.credential_action == "CLEAR":
                values["secret_id"] = None
                safety_connection_changed = True

            if safety_connection_changed:
                values.update(
                    connection_status=ProbeStatus.UNTESTED.value, capabilities={}, enabled=False
                )
            if not values:
                if expected_version != current.version:
                    raise self._version_conflict()
                return self._view(current)

            try:
                if not repository.update_config(
                    downloader_id,
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
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise self._name_conflict() from exc
            refreshed = self._require_record(repository, downloader_id)
            return self._view(refreshed)

    def delete(self, downloader_id: str, *, expected_version: int) -> None:
        with self._session_factory() as session:
            repository = DownloaderRepository(session)
            current = self._require_record(repository, downloader_id)
            if repository.task_count(downloader_id) > 0:
                raise ApplicationError(
                    code="DOWNLOADER_IN_USE",
                    status=409,
                    title="下载器仍被任务引用",
                    detail="仍有任务引用该下载器，不能删除",
                )
            if not repository.delete(downloader_id, expected_version=expected_version):
                session.rollback()
                raise self._version_conflict()
            if current.secret_id is not None:
                self._secret_store.delete_in_session(session, current.secret_id)
            session.commit()

    def set_enabled(
        self,
        downloader_id: str,
        *,
        expected_version: int,
        enabled: bool,
    ) -> DownloaderView:
        with self._session_factory() as session:
            repository = DownloaderRepository(session)
            current = self._require_record(repository, downloader_id)
            if enabled:
                if current.secret_id is None:
                    raise ApplicationError(
                        code="DOWNLOADER_CREDENTIAL_REQUIRED",
                        status=409,
                        title="下载器凭证未配置",
                        detail="启用自动化前必须绑定下载器凭证",
                    )
                if current.connection_status != ProbeStatus.OK.value:
                    raise ApplicationError(
                        code="DOWNLOADER_CONNECTION_TEST_REQUIRED",
                        status=409,
                        title="下载器连接尚未验证",
                        detail="启用自动化前必须通过连接测试",
                    )
            if current.enabled == enabled:
                if current.version != expected_version:
                    raise self._version_conflict()
                return self._view(current)
            if not repository.update_config(
                downloader_id,
                expected_version=expected_version,
                values={"enabled": enabled},
            ):
                session.rollback()
                raise self._version_conflict()
            session.commit()
            return self._view(self._require_record(repository, downloader_id))

    async def test_connection(self, downloader_id: str) -> dict[str, object]:
        snapshot = self._connection_snapshot(downloader_id)
        credential = (
            self._decode_credential(self._secret_store.get(snapshot.secret_id))
            if snapshot.secret_id is not None
            else None
        )
        adapter = self._adapter_factory.create(
            kind=snapshot.kind,
            base_url=snapshot.base_url,
            credential=credential,
        )
        tested_at = datetime.now(UTC)
        try:
            result = await adapter.test_connection()
        except DownloaderAdapterError as exc:
            self._store_connection_probe(
                snapshot.id,
                snapshot.version,
                status=ProbeStatus.FAILED,
                capabilities={},
                tested_at=tested_at,
            )
            raise ApplicationError(
                code=exc.code,
                status=502,
                title="下载器连接测试失败",
                detail=str(exc),
            ) from exc
        capabilities = result.capabilities.as_dict()
        self._store_connection_probe(
            snapshot.id,
            snapshot.version,
            status=ProbeStatus.OK,
            capabilities=capabilities,
            tested_at=tested_at,
        )
        return {"status": "ok", "capabilities": capabilities}

    async def probe_connection(
        self,
        *,
        kind: DownloaderKind,
        base_url: str,
        credential: DownloaderCredential | None,
    ) -> dict[str, object]:
        normalized_url = self._normalize_url(kind, base_url)
        self._validate_credential(kind, credential)
        adapter = self._adapter_factory.create(
            kind=kind,
            base_url=normalized_url,
            credential=credential,
        )
        try:
            result = await adapter.test_connection()
        except DownloaderAdapterError as exc:
            raise ApplicationError(
                code=exc.code,
                status=502,
                title="下载器连接测试失败",
                detail=str(exc),
            ) from exc
        return {"status": "ok", "capabilities": result.capabilities.as_dict()}

    async def runtime_metrics(self, downloader_id: str) -> DownloaderRuntimeMetrics:
        snapshot = self._connection_snapshot(downloader_id)
        credential = (
            self._decode_credential(self._secret_store.get(snapshot.secret_id))
            if snapshot.secret_id is not None
            else None
        )
        adapter = self._adapter_factory.create(
            kind=snapshot.kind,
            base_url=snapshot.base_url,
            credential=credential,
        )
        try:
            return await adapter.runtime_metrics()
        except DownloaderAdapterError as exc:
            raise ApplicationError(
                code=exc.code,
                status=502,
                title="下载器运行指标获取失败",
                detail=str(exc),
            ) from exc

    def list_tasks(self, downloader_id: str) -> list[dict[str, object]]:
        with self._session_factory() as session:
            repository = DownloaderRepository(session)
            self._require_record(repository, downloader_id)
            return [self._task_view(task) for task in repository.list_tasks(downloader_id)]

    @dataclass(frozen=True, slots=True)
    class _ConnectionSnapshot:
        id: str
        version: int
        kind: DownloaderKind
        base_url: str
        secret_id: str | None

    def _connection_snapshot(self, downloader_id: str) -> _ConnectionSnapshot:
        with self._session_factory() as session:
            record = self._require_record(DownloaderRepository(session), downloader_id)
            return self._ConnectionSnapshot(
                id=record.id,
                version=record.version,
                kind=DownloaderKind(record.type),
                base_url=record.base_url,
                secret_id=record.secret_id,
            )

    def _store_connection_probe(
        self,
        downloader_id: str,
        expected_version: int,
        *,
        status: ProbeStatus,
        capabilities: dict[str, Any],
        tested_at: datetime,
    ) -> None:
        with self._session_factory() as session:
            if not DownloaderRepository(session).update_connection_probe(
                downloader_id,
                expected_version=expected_version,
                status=status.value,
                capabilities=capabilities,
                tested_at=tested_at,
            ):
                session.rollback()
                raise self._version_conflict()
            session.commit()

    def _normalize_mappings(self, mappings: list[PathMappingRule]) -> list[PathMappingRule]:
        try:
            return normalize_path_mappings(mappings)
        except DomainViolation as exc:
            raise ApplicationError(
                code=exc.code.value,
                status=422,
                title="路径映射无效",
                detail=str(exc),
            ) from exc

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ApplicationError(
                code="DOWNLOADER_NAME_INVALID",
                status=422,
                title="下载器名称无效",
                detail="下载器名称不能为空且最长 80 个字符",
            )
        return normalized

    @staticmethod
    def _normalize_url(kind: DownloaderKind, value: str) -> str:
        try:
            return normalize_base_url(kind, value)
        except ValueError as exc:
            raise ApplicationError(
                code="DOWNLOADER_URL_INVALID",
                status=422,
                title="下载器管理地址无效",
                detail=str(exc),
            ) from exc

    def _validate_credential(
        self,
        kind: DownloaderKind,
        credential: DownloaderCredential | None,
    ) -> None:
        try:
            validate_credential(kind, credential)
        except ValueError as exc:
            raise self._credential_invalid(str(exc)) from exc

    @staticmethod
    def _encode_credential(credential: DownloaderCredential) -> bytes:
        return json.dumps(
            {
                "username": credential.username,
                "password": credential.password,
                "api_key": credential.api_key,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def _decode_credential(self, value: bytes) -> DownloaderCredential:
        try:
            payload = json.loads(value.decode())
            if not isinstance(payload, dict):
                raise ValueError
            for key in ("username", "password", "api_key"):
                item = payload.get(key)
                if item is not None and not isinstance(item, str):
                    raise ValueError
            return DownloaderCredential(
                username=payload.get("username"),
                password=payload.get("password"),
                api_key=payload.get("api_key"),
            )
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                code="DOWNLOADER_CREDENTIAL_INVALID",
                status=500,
                title="下载器凭证不可用",
                detail="加密下载器凭证格式无效，需要重新配置",
            ) from exc

    @staticmethod
    def _mapping_json(mappings: list[PathMappingRule]) -> list[dict[str, str]]:
        return [
            {"remote_prefix": rule.remote_prefix, "container_prefix": rule.container_prefix}
            for rule in mappings
        ]

    @staticmethod
    def _record_mappings(record: Downloader) -> list[PathMappingRule]:
        return [
            PathMappingRule(
                remote_prefix=item["remote_prefix"],
                container_prefix=item["container_prefix"],
            )
            for item in record.path_mappings
        ]

    def _view(self, record: Downloader) -> DownloaderView:
        return DownloaderView(
            id=record.id,
            name=record.name,
            type=DownloaderKind(record.type),
            base_url=record.base_url,
            credential_configured=record.secret_id is not None,
            monitor_rules=dict(record.monitor_rules),
            path_mappings=tuple(self._record_mappings(record)),
            capabilities=dict(record.capabilities),
            connection_status=ProbeStatus(record.connection_status),
            enabled=record.enabled,
            version=record.version,
            last_test_at=record.last_test_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _task_view(task: UnpackTask) -> dict[str, object]:
        return {
            "id": task.id,
            "type": task.type,
            "source_hash": task.source_hash,
            "status": task.status,
            "version": task.version,
            "updated_at": task.updated_at.isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    def _require_record(repository: DownloaderRepository, downloader_id: str) -> Downloader:
        record = repository.get(downloader_id)
        if record is None:
            raise ApplicationError(
                code="DOWNLOADER_NOT_FOUND",
                status=404,
                title="下载器不存在",
                detail="指定的下载器配置不存在",
            )
        return record

    @staticmethod
    def _credential_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="DOWNLOADER_CREDENTIAL_INVALID",
            status=422,
            title="下载器凭证无效",
            detail=detail,
        )

    @staticmethod
    def _name_conflict() -> ApplicationError:
        return ApplicationError(
            code="DOWNLOADER_NAME_CONFLICT",
            status=409,
            title="下载器名称已存在",
            detail="下载器名称必须唯一",
        )

    @staticmethod
    def _version_conflict() -> ApplicationError:
        return ApplicationError(
            code="DOWNLOADER_VERSION_CONFLICT",
            status=412,
            title="下载器配置版本冲突",
            detail="下载器配置已经变化，请刷新后重试",
        )
