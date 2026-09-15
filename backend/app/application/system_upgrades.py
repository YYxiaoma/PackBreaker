from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.app.application.backup_schedule import BackupRunReport
from backend.app.application.errors import ApplicationError
from backend.app.config import AppSettings
from backend.app.infrastructure.backups import BackupError
from backend.app.infrastructure.release_preflight import (
    ReleasePreflightReport,
    run_release_preflight,
)
from backend.app.infrastructure.release_updates import (
    OFFICIAL_IMAGE,
    ReleaseTarget,
    ReleaseUpdateClient,
    ReleaseUpdateError,
    semantic_version,
)
from backend.app.infrastructure.updater_protocol import (
    UpdaterClient,
    UpdaterProtocolError,
    UpdaterStatus,
    UpgradeHelperRequest,
)
from backend.app.versioning import app_version


@dataclass(frozen=True, slots=True)
class SystemUpgradeStatus:
    current_version: str
    latest_version: str | None
    update_available: bool
    target_tag: str | None
    target_image_digest: str | None
    immutable_image: str | None
    platform: str | None
    release_error_code: str | None
    helper_available: bool
    helper_status: UpdaterStatus | None
    can_upgrade: bool
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SystemUpgradeActionResult:
    request_id: str
    current_version: str
    target_version: str
    target_image: str
    backup_database_file: str | None
    helper_status: UpdaterStatus
    idempotency_replayed: bool


class ReleaseProvider(Protocol):
    def latest(self) -> ReleaseTarget: ...


class UpdaterGateway(Protocol):
    def status(self) -> UpdaterStatus: ...

    def start_upgrade(self, request: UpgradeHelperRequest) -> UpdaterStatus: ...


class BackupRunner(Protocol):
    async def run_once(self, *, force: bool = False) -> BackupRunReport | None: ...


class PreflightRunner(Protocol):
    def __call__(
        self,
        settings: AppSettings,
        *,
        app_version: str,
        exercise_backup: bool = True,
    ) -> ReleasePreflightReport: ...


class SystemUpgradeService:
    def __init__(
        self,
        settings: AppSettings,
        *,
        release_client: ReleaseProvider | None = None,
        updater_client: UpdaterGateway | None = None,
        preflight_runner: PreflightRunner = run_release_preflight,
        main_docker_socket_path: Path = Path("/var/run/docker.sock"),
    ) -> None:
        self._settings = settings
        self._release_client = release_client or ReleaseUpdateClient()
        self._updater_client = updater_client or UpdaterClient(
            settings.updater_socket_path,
            settings.updater_token_path,
        )
        self._main_docker_socket_path = main_docker_socket_path
        self._preflight_runner = preflight_runner

    async def status(self) -> SystemUpgradeStatus:
        current_version = app_version()
        latest: ReleaseTarget | None = None
        release_error_code: str | None = None
        try:
            latest = await asyncio.to_thread(self._release_client.latest)
        except ReleaseUpdateError as exc:
            release_error_code = exc.code

        helper: UpdaterStatus | None = None
        helper_available = False
        try:
            helper = await asyncio.to_thread(self._updater_client.status)
            helper_available = True
        except UpdaterProtocolError:
            pass

        blocked: list[str] = []
        if self._main_docker_socket_path.exists():
            blocked.append("MAIN_DOCKER_SOCKET_PRESENT")
        if not helper_available:
            blocked.append("UPDATER_HELPER_UNAVAILABLE")
        elif helper is not None and helper.active:
            blocked.append("UPDATER_BUSY")
        elif helper is not None and helper.phase == "manual_recovery_required":
            blocked.append("UPDATER_MANUAL_RECOVERY_REQUIRED")
        if latest is None:
            blocked.append(release_error_code or "RELEASE_TARGET_UNAVAILABLE")
            update_available = False
        else:
            try:
                update_available = semantic_version(latest.version) > semantic_version(
                    current_version
                )
            except ReleaseUpdateError:
                update_available = False
                blocked.append("CURRENT_VERSION_INVALID")
            if not update_available:
                blocked.append("NO_NEWER_RELEASE")

        return SystemUpgradeStatus(
            current_version=current_version,
            latest_version=None if latest is None else latest.version,
            update_available=update_available,
            target_tag=None if latest is None else latest.tag,
            target_image_digest=None if latest is None else latest.image_digest,
            immutable_image=None if latest is None else latest.immutable_image,
            platform=None if latest is None else latest.platform,
            release_error_code=release_error_code,
            helper_available=helper_available,
            helper_status=helper,
            can_upgrade=update_available and not blocked,
            blocked_reasons=tuple(dict.fromkeys(blocked)),
        )

    async def execute(
        self,
        *,
        target_version: str,
        target_image_digest: str,
        idempotency_key: str | None,
        backup_driver: BackupRunner,
    ) -> SystemUpgradeActionResult:
        request_id = _require_idempotency_key(idempotency_key)
        current_version = app_version()
        if self._main_docker_socket_path.exists():
            raise ApplicationError(
                code="UPGRADE_MAIN_DOCKER_SOCKET_PRESENT",
                status=409,
                title="主容器权限过高",
                detail=(
                    "自动升级要求 docker.sock 只挂载到独立 updater helper，"
                    "请先移除主 PackBreaker 容器的 Docker socket 挂载"
                ),
            )
        try:
            helper = await asyncio.to_thread(self._updater_client.status)
        except UpdaterProtocolError as exc:
            raise _updater_error(exc) from exc
        if helper.request_id == request_id:
            expected_target_image = f"{OFFICIAL_IMAGE}@{target_image_digest.lower()}"
            if (
                helper.target_version != target_version
                or helper.target_image != expected_target_image
            ):
                raise ApplicationError(
                    code="UPGRADE_IDEMPOTENCY_CONFLICT",
                    status=409,
                    title="升级幂等键冲突",
                    detail="相同 Idempotency-Key 已用于不同升级目标",
                )
            return SystemUpgradeActionResult(
                request_id=request_id,
                current_version=helper.current_version or current_version,
                target_version=helper.target_version,
                target_image=helper.target_image,
                backup_database_file=helper.backup_database_file,
                helper_status=helper,
                idempotency_replayed=True,
            )
        if helper.active:
            raise ApplicationError(
                code="UPGRADE_BUSY",
                status=409,
                title="已有升级正在执行",
                detail="独立 updater helper 当前正在处理另一个升级请求",
            )
        if helper.phase == "manual_recovery_required":
            raise ApplicationError(
                code="UPGRADE_MANUAL_RECOVERY_REQUIRED",
                status=409,
                title="升级现场需要人工恢复",
                detail="上一次升级未能自动收敛，禁止继续自动升级覆盖现场",
            )

        try:
            target = await asyncio.to_thread(self._release_client.latest)
        except ReleaseUpdateError as exc:
            raise ApplicationError(
                code=exc.code,
                status=503,
                title="无法确认正式升级目标",
                detail=str(exc),
            ) from exc
        if target.version != target_version or target.image_digest != target_image_digest.lower():
            raise ApplicationError(
                code="UPGRADE_TARGET_STALE",
                status=409,
                title="升级目标已变化",
                detail="页面中的目标版本或 digest 已不是当前最新正式 Release，请刷新升级中心后重试",
            )
        if semantic_version(target.version) <= semantic_version(current_version):
            raise ApplicationError(
                code="UPGRADE_NOT_NEWER",
                status=409,
                title="没有可升级的新版本",
                detail="目标正式版本不高于当前运行版本",
            )

        preflight = await asyncio.to_thread(
            self._preflight_runner,
            self._settings,
            app_version=current_version,
            exercise_backup=True,
        )
        blocked_codes = [check.code for check in preflight.checks if check.status == "blocked"]
        if blocked_codes:
            raise ApplicationError(
                code="UPGRADE_PREFLIGHT_BLOCKED",
                status=409,
                title="升级前置检查未通过",
                detail=f"阻断项：{', '.join(blocked_codes)}",
            )

        try:
            backup = await backup_driver.run_once(force=True)
        except (BackupError, OSError) as exc:
            raise ApplicationError(
                code="UPGRADE_BACKUP_FAILED",
                status=503,
                title="升级前备份失败",
                detail="未创建升级前一致性备份，因此没有启动 Docker 升级",
            ) from exc
        if backup is None or not backup.created or backup.database_file is None:
            raise ApplicationError(
                code="UPGRADE_BACKUP_UNAVAILABLE",
                status=409,
                title="升级前备份不可用",
                detail="未取得可用于回滚的升级前一致性备份，因此没有启动 Docker 升级",
            )
        database_file = backup.database_file
        manifest_file = f"{Path(database_file).stem}.json"
        request = UpgradeHelperRequest(
            request_id=request_id,
            current_version=current_version,
            target_version=target.version,
            target_image=target.immutable_image,
            backup_database_file=database_file,
            backup_manifest_file=manifest_file,
        )
        try:
            accepted = await asyncio.to_thread(self._updater_client.start_upgrade, request)
        except UpdaterProtocolError as exc:
            raise _updater_error(exc) from exc
        return SystemUpgradeActionResult(
            request_id=request_id,
            current_version=current_version,
            target_version=target.version,
            target_image=target.immutable_image,
            backup_database_file=database_file,
            helper_status=accepted,
            idempotency_replayed=False,
        )


def _require_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            status=428,
            title="缺少幂等键",
            detail="Docker 升级请求必须携带 Idempotency-Key",
        )
    if not value or len(value) > 200 or value.strip() != value:
        raise _invalid_idempotency_key()
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise _invalid_idempotency_key()
    return value


def _invalid_idempotency_key() -> ApplicationError:
    return ApplicationError(
        code="IDEMPOTENCY_KEY_INVALID",
        status=422,
        title="幂等键无效",
        detail="Idempotency-Key 必须是 1..200 个不含空白/控制字符的可见 ASCII 字符",
    )


def _updater_error(exc: UpdaterProtocolError) -> ApplicationError:
    status = 409 if exc.code in {"UPDATER_BUSY", "UPDATER_IDEMPOTENCY_CONFLICT"} else 503
    return ApplicationError(
        code=exc.code,
        status=status,
        title="独立升级 helper 不可用",
        detail=str(exc),
    )
