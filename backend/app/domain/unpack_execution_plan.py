from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from backend.app.domain.execution_plan import ExecutionPlanBlockReason
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.verification import FileSnapshot, VerificationLevel

UNPACK_EXECUTION_PLAN_SCHEMA_VERSION = "packbreaker-unpack-execution-plan-v1"


class UnpackExecutionActionKind(StrEnum):
    MATERIALIZE = "MATERIALIZE"
    PROTOCOL_PADDING = "PROTOCOL_PADDING"
    ZERO_LENGTH = "ZERO_LENGTH"


@dataclass(frozen=True, slots=True)
class UnpackExecutionAction:
    torrent_path: str
    kind: UnpackExecutionActionKind
    length: int
    source_path: str | None = None
    source_snapshot: FileSnapshot | None = None
    reuse_target_snapshot: FileSnapshot | None = None
    reuse_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "torrent_path", _safe_relative_path(self.torrent_path))
        if self.length < 0:
            raise ValueError("执行计划文件长度不能为负数")
        if self.kind is UnpackExecutionActionKind.MATERIALIZE:
            if not self.source_path or self.source_snapshot is None:
                raise ValueError("MATERIALIZE 动作必须绑定源路径与源快照")
            if self.source_snapshot.size != self.length:
                raise ValueError("MATERIALIZE 长度必须与源快照一致")
        elif self.source_path is not None or self.source_snapshot is not None:
            raise ValueError("非 MATERIALIZE 动作不能绑定源文件")
        if self.reuse_target_snapshot is None:
            if self.reuse_sha256 is not None:
                raise ValueError("没有冻结目标快照不能声明复用摘要")
        else:
            if self.kind is not UnpackExecutionActionKind.MATERIALIZE:
                raise ValueError("只有 MATERIALIZE 可安全复用已有文件")
            if (
                self.reuse_target_snapshot.file_type != "regular"
                or self.reuse_target_snapshot.size != self.length
            ):
                raise ValueError("冻结复用目标必须是等长普通文件")
            if self.source_snapshot is None:
                raise ValueError("复用目标缺少源快照")
            same_inode = (
                self.source_snapshot.device == self.reuse_target_snapshot.device
                and self.source_snapshot.inode == self.reuse_target_snapshot.inode
            )
            if same_inode and self.reuse_sha256 is not None:
                raise ValueError("同 inode 复用无需独立内容指纹")
            if not same_inode and (
                self.reuse_sha256 is None
                or len(self.reuse_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.reuse_sha256)
            ):
                raise ValueError("不同 inode 复用必须冻结完整 SHA-256")


@dataclass(frozen=True, slots=True)
class UnpackExecutionPlan:
    item_id: str
    item_version_before: int
    candidate_id: str
    candidate_generation: int
    metainfo_digest: str
    verification_level: VerificationLevel
    output_directory: str
    storage_mode: TaskStorageMode
    conflict_policy: TaskConflictPolicy
    target_device: int
    target_downloader_id: str
    target_downloader_version: int
    target_downloader_binding_digest: str
    target_remote_save_path: str
    client_check_required: bool
    actions: tuple[UnpackExecutionAction, ...]
    create_directories: tuple[str, ...]
    blocked_reasons: tuple[ExecutionPlanBlockReason, ...]
    created_at: datetime
    schema_version: str = UNPACK_EXECUTION_PLAN_SCHEMA_VERSION
    plan_digest: str = ""

    def __post_init__(self) -> None:
        if not self.item_id or self.item_version_before < 1:
            raise ValueError("执行计划必须绑定有效 item/version")
        if not self.candidate_id or self.candidate_generation < 0:
            raise ValueError("执行计划必须绑定有效候选")
        if len(self.metainfo_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.metainfo_digest
        ):
            raise ValueError("执行计划 metainfo digest 无效")
        if self.verification_level is not VerificationLevel.FULL_VERIFIED:
            raise ValueError("v2 最终执行计划只能消费 FULL_VERIFIED 候选")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("执行计划创建时间必须带时区")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if not self.output_directory:
            raise ValueError("执行计划必须绑定输出目录")
        if self.target_device < 0:
            raise ValueError("执行计划 target device 无效")
        if not self.target_downloader_id or self.target_downloader_version < 1:
            raise ValueError("执行计划必须绑定目标下载器")
        if len(self.target_downloader_binding_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.target_downloader_binding_digest
        ):
            raise ValueError("执行计划下载器 binding digest 无效")
        if not self.target_remote_save_path:
            raise ValueError("执行计划必须绑定下载器 save path")
        paths = tuple(item.torrent_path for item in self.actions)
        if len(set(paths)) != len(paths):
            raise ValueError("执行计划不能包含重复 torrent path")
        if any(
            item.kind is UnpackExecutionActionKind.MATERIALIZE and item.source_snapshot is None
            for item in self.actions
        ):
            raise ValueError("执行计划包含不完整 MATERIALIZE 动作")
        object.__setattr__(
            self,
            "create_directories",
            tuple(sorted({_safe_relative_path(item) for item in self.create_directories})),
        )
        object.__setattr__(
            self,
            "blocked_reasons",
            tuple(sorted(set(self.blocked_reasons), key=lambda item: item.value)),
        )
        payload = unpack_execution_plan_to_payload(self, include_digest=False)
        payload.pop("created_at", None)
        digest = sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.plan_digest and self.plan_digest != digest:
            raise ValueError("执行计划 digest 与内容不一致")
        object.__setattr__(self, "plan_digest", digest)

    @property
    def ready(self) -> bool:
        return not self.blocked_reasons


def unpack_execution_plan_to_payload(
    plan: UnpackExecutionPlan,
    *,
    include_digest: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": plan.schema_version,
        "item_id": plan.item_id,
        "item_version_before": plan.item_version_before,
        "candidate_id": plan.candidate_id,
        "candidate_generation": plan.candidate_generation,
        "metainfo_digest": plan.metainfo_digest,
        "verification_level": plan.verification_level.value,
        "output_directory": plan.output_directory,
        "storage_mode": plan.storage_mode.value,
        "conflict_policy": plan.conflict_policy.value,
        "target_device": plan.target_device,
        "target_downloader_id": plan.target_downloader_id,
        "target_downloader_version": plan.target_downloader_version,
        "target_downloader_binding_digest": plan.target_downloader_binding_digest,
        "target_remote_save_path": plan.target_remote_save_path,
        "client_check_required": plan.client_check_required,
        "actions": [_action_payload(item) for item in plan.actions],
        "create_directories": list(plan.create_directories),
        "blocked_reasons": [item.value for item in plan.blocked_reasons],
        "ready": plan.ready,
        "created_at": plan.created_at.isoformat(),
    }
    if include_digest:
        payload["plan_digest"] = plan.plan_digest
    return payload


def unpack_execution_plan_from_payload(payload: object) -> UnpackExecutionPlan:
    if not isinstance(payload, dict):
        raise ValueError("执行计划 payload 必须是对象")
    if payload.get("schema_version") != UNPACK_EXECUTION_PLAN_SCHEMA_VERSION:
        raise ValueError("执行计划 schema version 无效")
    raw_actions = payload.get("actions")
    raw_dirs = payload.get("create_directories")
    raw_blockers = payload.get("blocked_reasons")
    if (
        not isinstance(raw_actions, list)
        or not isinstance(raw_dirs, list)
        or not all(isinstance(item, str) for item in raw_dirs)
        or not isinstance(raw_blockers, list)
        or not all(isinstance(item, str) for item in raw_blockers)
    ):
        raise ValueError("执行计划集合字段无效")
    actions = tuple(_action_from_payload(item) for item in raw_actions)
    try:
        return UnpackExecutionPlan(
            item_id=_required_text(payload, "item_id"),
            item_version_before=_required_int(payload, "item_version_before"),
            candidate_id=_required_text(payload, "candidate_id"),
            candidate_generation=_required_int(payload, "candidate_generation"),
            metainfo_digest=_required_text(payload, "metainfo_digest"),
            verification_level=VerificationLevel(_required_text(payload, "verification_level")),
            output_directory=_required_text(payload, "output_directory"),
            storage_mode=TaskStorageMode(_required_text(payload, "storage_mode")),
            conflict_policy=TaskConflictPolicy(_required_text(payload, "conflict_policy")),
            target_device=_required_int(payload, "target_device"),
            target_downloader_id=_required_text(payload, "target_downloader_id"),
            target_downloader_version=_required_int(payload, "target_downloader_version"),
            target_downloader_binding_digest=_required_text(
                payload, "target_downloader_binding_digest"
            ),
            target_remote_save_path=_required_text(payload, "target_remote_save_path"),
            client_check_required=_required_bool(payload, "client_check_required"),
            actions=actions,
            create_directories=tuple(raw_dirs),
            blocked_reasons=tuple(ExecutionPlanBlockReason(item) for item in raw_blockers),
            created_at=datetime.fromisoformat(_required_text(payload, "created_at")),
            plan_digest=_required_text(payload, "plan_digest"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("执行计划 payload 字段无效") from exc


def _action_payload(action: UnpackExecutionAction) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "torrent_path": action.torrent_path,
        "kind": action.kind.value,
        "length": action.length,
        "source_path": action.source_path,
        "source_snapshot": _snapshot_payload(action.source_snapshot),
    }
    # Omit unused new fields to keep previously frozen plan digests valid.
    if action.reuse_target_snapshot is not None:
        payload["reuse_target_snapshot"] = _snapshot_payload(action.reuse_target_snapshot)
        payload["reuse_sha256"] = action.reuse_sha256
    return payload


def _action_from_payload(value: object) -> UnpackExecutionAction:
    if not isinstance(value, dict):
        raise ValueError("执行计划 action 必须是对象")
    return UnpackExecutionAction(
        torrent_path=_required_text(value, "torrent_path"),
        kind=UnpackExecutionActionKind(_required_text(value, "kind")),
        length=_required_int(value, "length"),
        source_path=_optional_text(value.get("source_path")),
        source_snapshot=_snapshot_from_payload(value.get("source_snapshot")),
        reuse_target_snapshot=_snapshot_from_payload(value.get("reuse_target_snapshot")),
        reuse_sha256=_optional_text(value.get("reuse_sha256")),
    )


def _snapshot_payload(snapshot: FileSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "device": snapshot.device,
        "inode": snapshot.inode,
        "size": snapshot.size,
        "mtime_ns": str(snapshot.mtime_ns),
        "file_type": snapshot.file_type,
    }


def _snapshot_from_payload(value: object) -> FileSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("源文件快照必须是对象")
    device = value.get("device")
    inode = value.get("inode")
    size = value.get("size")
    mtime_ns = value.get("mtime_ns")
    file_type = value.get("file_type")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not isinstance(mtime_ns, (str, int))
        or not isinstance(file_type, str)
    ):
        raise ValueError("源文件快照字段无效")
    return FileSnapshot(
        device=device,
        inode=inode,
        size=size,
        mtime_ns=int(mtime_ns),
        file_type=file_type,
    )


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("相对路径必须是文本")
    path = PurePosixPath(value.replace("\\", "/").strip())
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("执行计划包含不安全相对路径")
    normalized = "/".join(path.parts)
    if "\x00" in normalized:
        raise ValueError("执行计划路径包含 NUL")
    return normalized


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(key)
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(key)
    return value


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(key)
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("可选文本字段无效")
    return value
