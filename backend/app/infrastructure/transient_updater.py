from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from backend.app.infrastructure.docker_updater import (
    DockerEngineClient,
    DockerUpdaterError,
    build_replacement_plan,
)
from backend.app.infrastructure.updater_protocol import (
    UPDATER_PROTOCOL_VERSION,
    UpdaterProtocolError,
    UpdaterStatus,
    UpgradeHelperRequest,
    parse_updater_status,
)
from backend.app.versioning import app_version


class TransientUpdaterLauncher:
    """从主容器启动一次性 updater，升级完成后由 Docker 自动删除 helper。"""

    def __init__(
        self,
        *,
        config_dir: Path,
        docker_socket: Path = Path("/var/run/docker.sock"),
        target_container: str = "packbreaker",
        allowed_image: str = "ghcr.io/yyxiaoma/packbreaker",
    ) -> None:
        self._config_dir = config_dir
        self._docker_socket = docker_socket
        self._target_container = target_container
        self._allowed_image = allowed_image.lower()
        self._updater_dir = config_dir / "transient-updater"
        self._state_path = self._updater_dir / "state.json"
        self._requests_dir = self._updater_dir / "requests"

    @property
    def state_path(self) -> Path:
        return self._state_path

    def status(self) -> UpdaterStatus:
        self._require_docker_access()
        if not self._state_path.exists():
            return self._idle_status()
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            status = parse_updater_status(payload)
        except (OSError, json.JSONDecodeError, UpdaterProtocolError):
            return UpdaterStatus(
                protocol_version=UPDATER_PROTOCOL_VERSION,
                helper_version=app_version(),
                phase="manual_recovery_required",
                message="一次性 updater 状态文件损坏；请先核对 Docker 容器现场",
                updated_at=_now(),
                finished_at=_now(),
                rollback_performed=True,
            )
        return self._reconcile_active_status(status)

    def start_upgrade(self, request: UpgradeHelperRequest) -> UpdaterStatus:
        current = self.status()
        if current.request_id == request.request_id:
            if (
                current.target_image != request.target_image
                or current.target_version != request.target_version
            ):
                raise UpdaterProtocolError(
                    "UPDATER_IDEMPOTENCY_CONFLICT", "相同幂等键已用于不同升级目标"
                )
            return current
        if current.active:
            raise UpdaterProtocolError("UPDATER_BUSY", "已有一次性升级任务正在执行")
        if current.phase == "manual_recovery_required":
            raise UpdaterProtocolError(
                "UPDATER_MANUAL_RECOVERY_REQUIRED", "上一次升级现场需要人工核对"
            )

        request_path = self._write_request(request)
        accepted = UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version=app_version(),
            phase="accepted",
            message="一次性 updater 已接管升级；页面将短暂断开",
            request_id=request.request_id,
            current_version=request.current_version,
            target_version=request.target_version,
            target_image=request.target_image,
            backup_database_file=request.backup_database_file,
            started_at=_now(),
            updated_at=_now(),
        )
        self._write_status(accepted)

        helper_id: str | None = None
        try:
            with DockerEngineClient(self._docker_socket) as docker:
                inspected = docker.inspect_container(self._target_container)
                image_id = _required_string(inspected, "Image")
                old_image = docker.inspect_image(image_id)
                plan = build_replacement_plan(
                    inspected,
                    old_image,
                    target_image=request.target_image,
                    allowed_image=self._allowed_image,
                    preserve_docker_socket=True,
                )
                helper_name = self._helper_name(request.request_id)
                helper_id = docker.create_container(
                    helper_name,
                    self._helper_payload(
                        image_id=image_id,
                        config_bind=plan.config_bind,
                        request_path=request_path,
                    ),
                )
                docker.start_container(helper_id)
        except (DockerUpdaterError, httpx2.HTTPError, OSError) as exc:
            if helper_id is not None:
                try:
                    with DockerEngineClient(self._docker_socket) as docker:
                        docker.remove_container(helper_id, force=True)
                except (DockerUpdaterError, httpx2.HTTPError, OSError):
                    pass
            failed = replace(
                accepted,
                phase="failed",
                message="一次性 updater 启动失败；主 PackBreaker 未进入容器切换阶段",
                updated_at=_now(),
                finished_at=_now(),
            )
            self._write_status(failed)
            request_path.unlink(missing_ok=True)
            raise UpdaterProtocolError(
                "UPDATER_BOOTSTRAP_FAILED", "无法启动一次性 Docker updater"
            ) from exc
        return accepted

    def _require_docker_access(self) -> None:
        if not self._docker_socket.exists():
            raise UpdaterProtocolError(
                "UPDATER_DOCKER_SOCKET_REQUIRED",
                "单容器一键升级需要把 /var/run/docker.sock 挂载到 PackBreaker",
            )
        try:
            with DockerEngineClient(self._docker_socket) as docker:
                docker.inspect_container(self._target_container)
        except (DockerUpdaterError, httpx2.HTTPError, OSError) as exc:
            raise UpdaterProtocolError(
                "UPDATER_DOCKER_UNAVAILABLE", "PackBreaker 无法访问 Docker Engine 或自身容器"
            ) from exc

    def _reconcile_active_status(self, status: UpdaterStatus) -> UpdaterStatus:
        if not status.active or status.request_id is None:
            return status
        # start_upgrade 会先持久化 accepted，再创建 helper；给这一小段并发窗口留出余量，
        # 避免另一个 status 请求把尚未完成 bootstrap 的正常请求误判为 helper 丢失。
        if status.phase == "accepted" and _status_age_seconds(status) < 30:
            return status
        helper_name = self._helper_name(status.request_id)
        try:
            with DockerEngineClient(self._docker_socket) as docker:
                docker.inspect_container(helper_name)
            return status
        except (DockerUpdaterError, httpx2.HTTPError, OSError):
            now = _now()
            safe_before_stop = status.phase in {"accepted", "pulling"}
            reconciled = replace(
                status,
                phase="failed" if safe_before_stop else "manual_recovery_required",
                message=(
                    "一次性 updater 已退出且尚未进入主容器停机阶段，可重新发起升级"
                    if safe_before_stop
                    else "一次性 updater 在容器切换期间异常退出；请先核对 Docker 容器与备份现场"
                ),
                updated_at=now,
                finished_at=now,
                rollback_performed=not safe_before_stop,
            )
            self._write_status(reconciled)
            return reconciled

    def _write_request(self, request: UpgradeHelperRequest) -> Path:
        self._requests_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._requests_dir, 0o700)
        name = hashlib.sha256(request.request_id.encode("ascii")).hexdigest()[:24] + ".json"
        path = self._requests_dir / name
        serialized = json.dumps(request.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        content = serialized.encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        _fsync_directory(self._requests_dir)
        return path

    def _write_status(self, status: UpdaterStatus) -> None:
        self._updater_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._updater_dir, 0o700)
        temporary = self._state_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(status.as_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
            _fsync_directory(self._updater_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def _helper_payload(
        self,
        *,
        image_id: str,
        config_bind: str,
        request_path: Path,
    ) -> dict[str, Any]:
        in_container_request = f"/config/transient-updater/requests/{request_path.name}"
        return {
            "Image": image_id,
            "User": "0:0",
            "Entrypoint": ["python", "-m", "backend.app.updater_helper"],
            "Cmd": ["--oneshot", in_container_request],
            "Env": [
                "PACKBREAKER_CONFIG_DIR=/config",
                "PACKBREAKER_DOCKER_SOCKET=/var/run/docker.sock",
                f"PACKBREAKER_UPDATER_TARGET_CONTAINER={self._target_container}",
                f"PACKBREAKER_UPDATER_ALLOWED_IMAGE={self._allowed_image}",
                "PACKBREAKER_UPDATER_STATE_FILE=/config/transient-updater/state.json",
                "PACKBREAKER_UPDATER_PRESERVE_DOCKER_SOCKET=1",
            ],
            "Labels": {
                "com.packbreaker.updater.mode": "oneshot",
                "com.packbreaker.updater.target": self._target_container,
            },
            "HostConfig": {
                "Binds": [config_bind, f"{self._docker_socket}:/var/run/docker.sock"],
                "NetworkMode": "none",
                "AutoRemove": True,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            },
        }

    def _helper_name(self, request_id: str) -> str:
        suffix = hashlib.sha256(request_id.encode("ascii")).hexdigest()[:12]
        return f"{self._target_container}-updater-once-{suffix}"

    @staticmethod
    def _idle_status() -> UpdaterStatus:
        return UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version=app_version(),
            phase="idle",
            message="单容器一次性 updater 已就绪",
            updated_at=_now(),
        )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DockerUpdaterError("UPGRADE_CONTAINER_INVALID", f"Docker 字段 {key} 无效")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _status_age_seconds(status: UpdaterStatus) -> float:
    raw = status.updated_at or status.started_at
    if raw is None:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
