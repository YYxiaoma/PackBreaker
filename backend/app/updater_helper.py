from __future__ import annotations

import json
import os
import re
import secrets
import socketserver
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from backend.app.infrastructure.docker_updater import (
    DockerEngineClient,
    DockerUpdaterError,
    DockerUpgradeExecutor,
    digest_image_repository,
    image_repository_is_valid,
)
from backend.app.infrastructure.updater_protocol import (
    MAX_UPDATER_MESSAGE_BYTES,
    UPDATER_PROTOCOL_VERSION,
    UpdaterPhase,
    UpdaterStatus,
    UpgradeHelperRequest,
    secure_token_matches,
)
from backend.app.versioning import app_version

_BACKUP_DB = re.compile(r"^packbreaker-\d{8}T\d{6}Z-[0-9a-f]{8}\.db$")
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class HelperRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class UpdaterStateStore:
    def __init__(self, path: Path, *, helper_version: str) -> None:
        self._path = path
        self._helper_version = helper_version
        self._lock = threading.RLock()

    def initial(self) -> UpdaterStatus:
        with self._lock:
            state_file_existed = self._path.exists()
            state = self._read_unlocked()
            if state is None:
                if state_file_existed:
                    state = UpdaterStatus(
                        protocol_version=UPDATER_PROTOCOL_VERSION,
                        helper_version=self._helper_version,
                        phase="manual_recovery_required",
                        message=(
                            "升级 helper 状态文件损坏或不可读取；为避免覆盖升级现场，已要求人工核对"
                        ),
                        updated_at=_now(),
                        finished_at=_now(),
                        rollback_performed=True,
                    )
                else:
                    state = UpdaterStatus(
                        protocol_version=UPDATER_PROTOCOL_VERSION,
                        helper_version=self._helper_version,
                        phase="idle",
                        message="升级 helper 已就绪",
                        updated_at=_now(),
                    )
                self._write_unlocked(state)
            elif state.active:
                state = replace(
                    state,
                    helper_version=self._helper_version,
                    phase="manual_recovery_required",
                    message=(
                        "升级 helper 在活动升级期间发生重启；已停止自动动作，"
                        "请人工核对 Docker 容器与备份状态"
                    ),
                    updated_at=_now(),
                    finished_at=_now(),
                    rollback_performed=True,
                )
                self._write_unlocked(state)
            return state

    def get(self) -> UpdaterStatus:
        with self._lock:
            return self._read_unlocked() or self.initial()

    def set(self, status: UpdaterStatus) -> UpdaterStatus:
        with self._lock:
            self._write_unlocked(status)
            return status

    def _read_unlocked(self) -> UpdaterStatus | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        phase = payload.get("phase")
        if phase not in {
            "idle",
            "accepted",
            "pulling",
            "stopping",
            "starting",
            "verifying",
            "succeeded",
            "rolling_back",
            "rolled_back",
            "failed",
            "manual_recovery_required",
        }:
            return None
        return UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version=self._helper_version,
            phase=cast(UpdaterPhase, phase),
            message=str(payload.get("message") or "升级状态不可用"),
            request_id=_maybe_str(payload.get("request_id")),
            current_version=_maybe_str(payload.get("current_version")),
            target_version=_maybe_str(payload.get("target_version")),
            target_image=_maybe_str(payload.get("target_image")),
            backup_database_file=_maybe_str(payload.get("backup_database_file")),
            started_at=_maybe_str(payload.get("started_at")),
            updated_at=_maybe_str(payload.get("updated_at")),
            finished_at=_maybe_str(payload.get("finished_at")),
            rollback_performed=bool(payload.get("rollback_performed", False)),
        )

    def _write_unlocked(self, status: UpdaterStatus) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = self._path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(status.as_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        finally:
            temporary.unlink(missing_ok=True)


class UpgradeCoordinator:
    def __init__(
        self,
        *,
        state_store: UpdaterStateStore,
        config_dir: Path,
        docker_socket: Path,
        target_container: str,
        allowed_image: str,
        helper_version: str,
    ) -> None:
        self._state_store = state_store
        self._config_dir = config_dir
        self._docker_socket = docker_socket
        self._target_container = target_container
        self._allowed_image = allowed_image.lower()
        self._helper_version = helper_version
        self._thread_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def status(self) -> UpdaterStatus:
        return self._state_store.get()

    def start(self, request: UpgradeHelperRequest) -> UpdaterStatus:
        current = self._state_store.get()
        if current.request_id == request.request_id:
            if (
                current.target_image != request.target_image
                or current.target_version != request.target_version
            ):
                raise HelperRequestError(
                    "UPDATER_IDEMPOTENCY_CONFLICT",
                    "相同幂等键已用于不同升级目标",
                )
            return current
        if current.active:
            raise HelperRequestError("UPDATER_BUSY", "已有升级任务正在执行")
        with self._thread_lock:
            if self._worker is not None and self._worker.is_alive():
                raise HelperRequestError("UPDATER_BUSY", "已有升级 worker 正在执行")
            now = _now()
            accepted = UpdaterStatus(
                protocol_version=UPDATER_PROTOCOL_VERSION,
                helper_version=self._helper_version,
                phase="accepted",
                message="升级请求已由独立 helper 接管",
                request_id=request.request_id,
                current_version=request.current_version,
                target_version=request.target_version,
                target_image=request.target_image,
                backup_database_file=request.backup_database_file,
                started_at=now,
                updated_at=now,
            )
            self._state_store.set(accepted)
            self._worker = threading.Thread(
                target=self._execute,
                args=(request,),
                name=f"packbreaker-upgrade-{request.request_id[:12]}",
                daemon=True,
            )
            self._worker.start()
            return accepted

    def _execute(self, request: UpgradeHelperRequest) -> None:
        time.sleep(request.grace_seconds)
        try:
            with DockerEngineClient(self._docker_socket) as docker:
                executor = DockerUpgradeExecutor(
                    docker,
                    target_container=self._target_container,
                    allowed_image=self._allowed_image,
                    config_dir=self._config_dir,
                )
                outcome = executor.execute(request, phase=self._phase)
        except Exception as exc:
            message = (
                f"{exc.code}:{exc}"
                if isinstance(exc, (DockerUpdaterError, HelperRequestError))
                else f"{type(exc).__name__}"
            )
            self._finish("failed", f"升级 helper 执行失败：{message}", rollback_performed=False)
            return
        self._finish(outcome.phase, outcome.message, rollback_performed=outcome.rollback_performed)

    def _phase(self, phase: str, message: str) -> None:
        current = self._state_store.get()
        self._state_store.set(
            replace(
                current,
                phase=cast(UpdaterPhase, phase),
                message=message,
                updated_at=_now(),
            )
        )

    def _finish(self, phase: str, message: str, *, rollback_performed: bool) -> None:
        current = self._state_store.get()
        now = _now()
        self._state_store.set(
            replace(
                current,
                phase=cast(UpdaterPhase, phase),
                message=message,
                updated_at=now,
                finished_at=now,
                rollback_performed=rollback_performed,
            )
        )


class _UpdaterUnixServer(socketserver.UnixStreamServer):
    coordinator: UpgradeCoordinator
    token: str


class _RequestHandler(socketserver.StreamRequestHandler):
    server: _UpdaterUnixServer

    def handle(self) -> None:
        raw = self.rfile.readline(MAX_UPDATER_MESSAGE_BYTES + 1)
        if len(raw) > MAX_UPDATER_MESSAGE_BYTES:
            self._write_error("UPDATER_REQUEST_TOO_LARGE", "请求超出大小限制")
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise HelperRequestError("UPDATER_REQUEST_INVALID", "请求必须是 JSON object")
            if payload.get("protocol_version") != UPDATER_PROTOCOL_VERSION:
                raise HelperRequestError("UPDATER_PROTOCOL_MISMATCH", "升级 helper 协议版本不兼容")
            if not secure_token_matches(self.server.token, payload.get("token")):
                raise HelperRequestError("UPDATER_AUTH_FAILED", "升级 helper 认证失败")
            operation = payload.get("operation")
            if operation == "status":
                status = self.server.coordinator.status()
            elif operation == "upgrade":
                status = self.server.coordinator.start(
                    _parse_upgrade_request(payload.get("request"))
                )
            else:
                raise HelperRequestError("UPDATER_OPERATION_INVALID", "不支持的升级 helper 操作")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_error("UPDATER_REQUEST_INVALID", "请求不是合法 JSON")
            return
        except HelperRequestError as exc:
            self._write_error(exc.code, str(exc))
            return
        self._write({"ok": True, "status": status.as_dict()})

    def _write_error(self, code: str, message: str) -> None:
        self._write({"ok": False, "code": code, "message": message})

    def _write(self, payload: dict[str, object]) -> None:
        self.wfile.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )
        self.wfile.flush()


def main() -> None:
    config_dir = Path(os.environ.get("PACKBREAKER_CONFIG_DIR", "/config")).resolve()
    docker_socket = Path(os.environ.get("PACKBREAKER_DOCKER_SOCKET", "/var/run/docker.sock"))
    target_container = os.environ.get("PACKBREAKER_UPDATER_TARGET_CONTAINER", "packbreaker").strip()
    allowed_image = (
        os.environ.get("PACKBREAKER_UPDATER_ALLOWED_IMAGE", "ghcr.io/yyxiaoma/packbreaker")
        .strip()
        .lower()
    )
    if _CONTAINER_NAME.fullmatch(target_container) is None:
        raise SystemExit("PACKBREAKER_UPDATER_TARGET_CONTAINER 格式无效")
    if not image_repository_is_valid(allowed_image):
        raise SystemExit("PACKBREAKER_UPDATER_ALLOWED_IMAGE 格式无效")
    if not docker_socket.exists():
        raise SystemExit("未检测到 Docker socket，升级 helper 无法启动")

    uid = _nonnegative_id("PUID", 1000)
    gid = _nonnegative_id("PGID", 1000)
    updater_dir = config_dir / "updater"
    updater_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(updater_dir, uid, gid)
    token_path = updater_dir / "token"
    token = _load_or_create_token(token_path, uid=uid, gid=gid)
    socket_path = updater_dir / "updater.sock"
    socket_path.unlink(missing_ok=True)
    helper_version = app_version()
    state_store = UpdaterStateStore(updater_dir / "state.json", helper_version=helper_version)
    state_store.initial()
    coordinator = UpgradeCoordinator(
        state_store=state_store,
        config_dir=config_dir,
        docker_socket=docker_socket,
        target_container=target_container,
        allowed_image=allowed_image,
        helper_version=helper_version,
    )
    server = _UpdaterUnixServer(str(socket_path), _RequestHandler)
    server.coordinator = coordinator
    server.token = token
    os.chmod(socket_path, 0o660)
    os.chown(socket_path, uid, gid)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


def _parse_upgrade_request(value: object) -> UpgradeHelperRequest:
    if not isinstance(value, dict):
        raise HelperRequestError("UPDATER_REQUEST_INVALID", "upgrade request 格式无效")
    request_id = _request_string(value, "request_id")
    if len(request_id) > 200 or any(ord(char) < 33 or ord(char) > 126 for char in request_id):
        raise HelperRequestError("UPDATER_IDEMPOTENCY_INVALID", "request_id 必须是可见 ASCII 字符")
    current_version = _version(value, "current_version")
    target_version = _version(value, "target_version")
    target_image = _request_string(value, "target_image").lower()
    if digest_image_repository(target_image) is None:
        raise HelperRequestError("UPDATER_TARGET_INVALID", "目标镜像必须使用不可变 sha256 digest")
    backup_database_file = _request_string(value, "backup_database_file")
    if _BACKUP_DB.fullmatch(backup_database_file) is None:
        raise HelperRequestError("UPDATER_BACKUP_INVALID", "升级前备份文件名无效")
    backup_manifest_file = _request_string(value, "backup_manifest_file")
    if backup_manifest_file != f"{Path(backup_database_file).stem}.json":
        raise HelperRequestError("UPDATER_BACKUP_INVALID", "升级前备份 manifest 与数据库不匹配")
    grace = value.get("grace_seconds", 2.0)
    if (
        not isinstance(grace, (int, float))
        or isinstance(grace, bool)
        or not 0.5 <= float(grace) <= 10
    ):
        raise HelperRequestError("UPDATER_GRACE_INVALID", "grace_seconds 必须位于 0.5..10 秒")
    return UpgradeHelperRequest(
        request_id=request_id,
        current_version=current_version,
        target_version=target_version,
        target_image=target_image,
        backup_database_file=backup_database_file,
        backup_manifest_file=backup_manifest_file,
        grace_seconds=float(grace),
    )


def _load_or_create_token(path: Path, *, uid: int, gid: int) -> str:
    if path.exists():
        token = path.read_text(encoding="ascii").strip()
        if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            raise SystemExit("updater token 文件格式无效")
        os.chmod(path, 0o600)
        os.chown(path, uid, gid)
        return token
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", closefd=True) as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(path, uid, gid)
    _fsync_directory(path.parent)
    return token


def _request_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise HelperRequestError("UPDATER_REQUEST_INVALID", f"upgrade request 字段 {key} 无效")
    return value


def _version(payload: dict[str, Any], key: str) -> str:
    value = _request_string(payload, key)
    if re.fullmatch(r"\d+\.\d+\.\d+", value) is None:
        raise HelperRequestError("UPDATER_VERSION_INVALID", f"{key} 必须使用 x.y.z")
    return value


def _nonnegative_id(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"{name} 必须是非负整数") from exc
    if value < 0:
        raise SystemExit(f"{name} 必须是非负整数")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _maybe_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
