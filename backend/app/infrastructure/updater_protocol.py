from __future__ import annotations

import hmac
import json
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

UPDATER_PROTOCOL_VERSION = 1
MAX_UPDATER_MESSAGE_BYTES = 64 * 1024
UpdaterPhase = Literal[
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
]
_ACTIVE_PHASES = frozenset(
    {"accepted", "pulling", "stopping", "starting", "verifying", "rolling_back"}
)


class UpdaterProtocolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class UpdaterStatus:
    protocol_version: int
    helper_version: str
    phase: UpdaterPhase
    message: str
    request_id: str | None = None
    current_version: str | None = None
    target_version: str | None = None
    target_image: str | None = None
    backup_database_file: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    rollback_performed: bool = False

    @property
    def active(self) -> bool:
        return self.phase in _ACTIVE_PHASES

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UpgradeHelperRequest:
    request_id: str
    current_version: str
    target_version: str
    target_image: str
    backup_database_file: str
    backup_manifest_file: str
    grace_seconds: float = 2.0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class UpdaterClient:
    def __init__(
        self,
        socket_path: Path,
        token_path: Path,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("updater client 超时必须大于 0")
        self._socket_path = socket_path
        self._token_path = token_path
        self._timeout_seconds = timeout_seconds

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def status(self) -> UpdaterStatus:
        response = self._request({"operation": "status"})
        return parse_updater_status(response.get("status"))

    def start_upgrade(self, request: UpgradeHelperRequest) -> UpdaterStatus:
        response = self._request({"operation": "upgrade", "request": request.as_dict()})
        return parse_updater_status(response.get("status"))

    def _request(self, payload: dict[str, object]) -> dict[str, Any]:
        try:
            token = self._token_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise UpdaterProtocolError(
                "UPDATER_TOKEN_UNAVAILABLE", "升级 helper 认证文件不可读取"
            ) from exc
        if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            raise UpdaterProtocolError("UPDATER_TOKEN_INVALID", "升级 helper 认证文件格式无效")
        envelope = {
            "protocol_version": UPDATER_PROTOCOL_VERSION,
            "token": token,
            **payload,
        }
        wire = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(wire) > MAX_UPDATER_MESSAGE_BYTES:
            raise UpdaterProtocolError("UPDATER_REQUEST_TOO_LARGE", "升级 helper 请求超出大小限制")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout_seconds)
                client.connect(str(self._socket_path))
                client.sendall(wire)
                response_wire = _read_line(client)
        except (OSError, TimeoutError) as exc:
            raise UpdaterProtocolError("UPDATER_UNAVAILABLE", "无法连接独立升级 helper") from exc
        try:
            response = json.loads(response_wire)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdaterProtocolError(
                "UPDATER_RESPONSE_INVALID", "升级 helper 返回了无效响应"
            ) from exc
        if not isinstance(response, dict):
            raise UpdaterProtocolError(
                "UPDATER_RESPONSE_INVALID", "升级 helper 响应必须是 JSON object"
            )
        if response.get("ok") is not True:
            code = response.get("code")
            message = response.get("message")
            raise UpdaterProtocolError(
                code if isinstance(code, str) else "UPDATER_REQUEST_FAILED",
                message if isinstance(message, str) else "升级 helper 拒绝了请求",
            )
        return response


def parse_updater_status(value: object) -> UpdaterStatus:
    if not isinstance(value, dict):
        raise UpdaterProtocolError("UPDATER_STATUS_INVALID", "升级 helper 状态格式无效")
    protocol = value.get("protocol_version")
    if protocol != UPDATER_PROTOCOL_VERSION:
        raise UpdaterProtocolError("UPDATER_PROTOCOL_MISMATCH", "升级 helper 协议版本不兼容")
    phase = value.get("phase")
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
        raise UpdaterProtocolError("UPDATER_STATUS_INVALID", "升级 helper phase 无效")
    return UpdaterStatus(
        protocol_version=protocol,
        helper_version=_required_text(value, "helper_version"),
        phase=phase,
        message=_required_text(value, "message"),
        request_id=_optional_text(value, "request_id"),
        current_version=_optional_text(value, "current_version"),
        target_version=_optional_text(value, "target_version"),
        target_image=_optional_text(value, "target_image"),
        backup_database_file=_optional_text(value, "backup_database_file"),
        started_at=_optional_text(value, "started_at"),
        updated_at=_optional_text(value, "updated_at"),
        finished_at=_optional_text(value, "finished_at"),
        rollback_performed=bool(value.get("rollback_performed", False)),
    )


def secure_token_matches(expected: str, actual: object) -> bool:
    return isinstance(actual, str) and hmac.compare_digest(expected, actual)


def _read_line(client: socket.socket) -> str:
    buffer = bytearray()
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_UPDATER_MESSAGE_BYTES:
            raise UpdaterProtocolError("UPDATER_RESPONSE_TOO_LARGE", "升级 helper 响应超出大小限制")
        if b"\n" in chunk:
            break
    if not buffer:
        raise UpdaterProtocolError("UPDATER_RESPONSE_EMPTY", "升级 helper 未返回响应")
    line = bytes(buffer).split(b"\n", 1)[0]
    return line.decode("utf-8")


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise UpdaterProtocolError("UPDATER_STATUS_INVALID", f"升级 helper 状态字段 {key} 无效")
    return value


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise UpdaterProtocolError("UPDATER_STATUS_INVALID", f"升级 helper 状态字段 {key} 无效")
    return value
