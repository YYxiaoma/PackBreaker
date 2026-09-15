from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.application.backup_schedule import BackupRunReport
from backend.app.application.errors import ApplicationError
from backend.app.application.system_upgrades import SystemUpgradeService
from backend.app.config import AppSettings
from backend.app.infrastructure.release_preflight import PreflightCheck, ReleasePreflightReport
from backend.app.infrastructure.release_updates import OFFICIAL_IMAGE, ReleaseTarget
from backend.app.infrastructure.updater_protocol import (
    UPDATER_PROTOCOL_VERSION,
    UpdaterStatus,
    UpgradeHelperRequest,
)

_DIGEST = "sha256:" + "8" * 64
_TARGET_IMAGE = f"{OFFICIAL_IMAGE}@{_DIGEST}"


class FakeReleaseProvider:
    def __init__(self, target: ReleaseTarget) -> None:
        self.target = target
        self.calls = 0

    def latest(self) -> ReleaseTarget:
        self.calls += 1
        return self.target


class FakeUpdaterGateway:
    def __init__(self, status: UpdaterStatus) -> None:
        self.current = status
        self.requests: list[UpgradeHelperRequest] = []

    def status(self) -> UpdaterStatus:
        return self.current

    def start_upgrade(self, request: UpgradeHelperRequest) -> UpdaterStatus:
        self.requests.append(request)
        self.current = UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.2",
            phase="accepted",
            message="accepted",
            request_id=request.request_id,
            current_version=request.current_version,
            target_version=request.target_version,
            target_image=request.target_image,
            backup_database_file=request.backup_database_file,
        )
        return self.current


class FakeBackupRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self, *, force: bool = False) -> BackupRunReport | None:
        assert force is True
        self.calls += 1
        return BackupRunReport(
            created=True,
            skipped_reason=None,
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
            database_file="packbreaker-20260915T010101Z-1234abcd.db",
            database_size_bytes=4096,
            retention_deleted_count=0,
            retention_blocked_count=0,
            retention_error_code=None,
        )


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def _target() -> ReleaseTarget:
    return ReleaseTarget(
        version="0.1.3",
        tag="v0.1.3",
        commit="9" * 40,
        image=OFFICIAL_IMAGE,
        image_digest=_DIGEST,
        immutable_image=_TARGET_IMAGE,
        platform="linux/amd64",
    )


def _idle() -> UpdaterStatus:
    return UpdaterStatus(
        protocol_version=UPDATER_PROTOCOL_VERSION,
        helper_version="0.1.2",
        phase="idle",
        message="ready",
    )


def _ready_preflight(
    settings: AppSettings, *, app_version: str, exercise_backup: bool = True
) -> ReleasePreflightReport:
    assert exercise_backup is True
    return ReleasePreflightReport(
        app_version=app_version,
        checks=(PreflightCheck("synthetic", "ok", "SYNTHETIC_OK", "ok"),),
    )


@pytest.mark.asyncio
async def test_status_reports_newer_release_and_ready_helper(tmp_path: Path) -> None:
    service = SystemUpgradeService(
        _settings(tmp_path),
        release_client=FakeReleaseProvider(_target()),
        updater_client=FakeUpdaterGateway(_idle()),
        main_docker_socket_path=tmp_path / "missing.sock",
    )

    status = await service.status()

    assert status.current_version == "0.1.2"
    assert status.latest_version == "0.1.3"
    assert status.update_available is True
    assert status.can_upgrade is True
    assert status.blocked_reasons == ()


@pytest.mark.asyncio
async def test_execute_replays_existing_helper_request_without_new_backup(tmp_path: Path) -> None:
    release = FakeReleaseProvider(_target())
    helper = FakeUpdaterGateway(
        UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.2",
            phase="accepted",
            message="accepted",
            request_id="same-key",
            current_version="0.1.2",
            target_version="0.1.3",
            target_image=_TARGET_IMAGE,
            backup_database_file="packbreaker-20260915T010101Z-1234abcd.db",
        )
    )
    backup = FakeBackupRunner()
    service = SystemUpgradeService(
        _settings(tmp_path),
        release_client=release,
        updater_client=helper,
        main_docker_socket_path=tmp_path / "missing.sock",
        preflight_runner=_ready_preflight,
    )

    result = await service.execute(
        target_version="0.1.3",
        target_image_digest=_DIGEST,
        idempotency_key="same-key",
        backup_driver=backup,
    )

    assert result.idempotency_replayed is True
    assert backup.calls == 0
    assert release.calls == 0
    assert helper.requests == []


@pytest.mark.asyncio
async def test_execute_rejects_same_idempotency_key_with_different_digest(tmp_path: Path) -> None:
    helper = FakeUpdaterGateway(
        UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.2",
            phase="accepted",
            message="accepted",
            request_id="same-key",
            current_version="0.1.2",
            target_version="0.1.3",
            target_image=_TARGET_IMAGE,
            backup_database_file="packbreaker-20260915T010101Z-1234abcd.db",
        )
    )
    backup = FakeBackupRunner()
    service = SystemUpgradeService(
        _settings(tmp_path),
        release_client=FakeReleaseProvider(_target()),
        updater_client=helper,
        main_docker_socket_path=tmp_path / "missing.sock",
        preflight_runner=_ready_preflight,
    )

    with pytest.raises(ApplicationError) as exc_info:
        await service.execute(
            target_version="0.1.3",
            target_image_digest="sha256:" + "7" * 64,
            idempotency_key="same-key",
            backup_driver=backup,
        )

    assert exc_info.value.code == "UPGRADE_IDEMPOTENCY_CONFLICT"
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_execute_rejects_stale_digest_before_backup(tmp_path: Path) -> None:
    backup = FakeBackupRunner()
    service = SystemUpgradeService(
        _settings(tmp_path),
        release_client=FakeReleaseProvider(_target()),
        updater_client=FakeUpdaterGateway(_idle()),
        main_docker_socket_path=tmp_path / "missing.sock",
        preflight_runner=_ready_preflight,
    )

    with pytest.raises(ApplicationError) as exc_info:
        await service.execute(
            target_version="0.1.3",
            target_image_digest="sha256:" + "7" * 64,
            idempotency_key="request-key",
            backup_driver=backup,
        )

    assert exc_info.value.code == "UPGRADE_TARGET_STALE"
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_execute_creates_backup_then_delegates_to_helper(tmp_path: Path) -> None:
    backup = FakeBackupRunner()
    helper = FakeUpdaterGateway(_idle())
    service = SystemUpgradeService(
        _settings(tmp_path),
        release_client=FakeReleaseProvider(_target()),
        updater_client=helper,
        main_docker_socket_path=tmp_path / "missing.sock",
        preflight_runner=_ready_preflight,
    )

    result = await service.execute(
        target_version="0.1.3",
        target_image_digest=_DIGEST,
        idempotency_key="request-key",
        backup_driver=backup,
    )

    assert result.idempotency_replayed is False
    assert backup.calls == 1
    assert len(helper.requests) == 1
    request = helper.requests[0]
    assert request.request_id == "request-key"
    assert request.target_image == _TARGET_IMAGE
    assert request.backup_manifest_file == "packbreaker-20260915T010101Z-1234abcd.json"


@pytest.mark.asyncio
async def test_execute_blocks_when_main_container_has_docker_socket(tmp_path: Path) -> None:
    docker_socket = tmp_path / "docker.sock"
    docker_socket.write_text("synthetic", encoding="utf-8")
    service = SystemUpgradeService(
        _settings(tmp_path),
        release_client=FakeReleaseProvider(_target()),
        updater_client=FakeUpdaterGateway(_idle()),
        main_docker_socket_path=docker_socket,
        preflight_runner=_ready_preflight,
    )

    with pytest.raises(ApplicationError) as exc_info:
        await service.execute(
            target_version="0.1.3",
            target_image_digest=_DIGEST,
            idempotency_key="request-key",
            backup_driver=FakeBackupRunner(),
        )

    assert exc_info.value.code == "UPGRADE_MAIN_DOCKER_SOCKET_PRESENT"
