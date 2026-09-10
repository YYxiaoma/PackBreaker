from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from backend.app.domain.verification import FileSnapshot, VerificationLevel

EXECUTION_PLAN_SCHEMA_VERSION = "packbreaker-execution-plan-v1"


class ExecutionPlanActionKind(StrEnum):
    HARDLINK = "HARDLINK"
    CLIENT_FETCH = "CLIENT_FETCH"
    PROTOCOL_PADDING = "PROTOCOL_PADDING"
    ZERO_LENGTH = "ZERO_LENGTH"


class ExecutionPlanBlockReason(StrEnum):
    TARGET_EXISTS = "TARGET_EXISTS"
    TARGET_PARENT_UNSAFE = "TARGET_PARENT_UNSAFE"
    CROSS_DEVICE = "CROSS_DEVICE"
    MAPPING_UNSUPPORTED = "MAPPING_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ExecutionPlanAction:
    torrent_path: str
    kind: ExecutionPlanActionKind
    length: int
    source_relative_path: str | None = None
    source_snapshot: FileSnapshot | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "torrent_path", _safe_relative_path(self.torrent_path))
        if self.length < 0:
            raise ValueError("execution plan action length 不能为负数")
        if self.source_relative_path is not None:
            object.__setattr__(
                self,
                "source_relative_path",
                _safe_relative_path(self.source_relative_path),
            )
        if self.kind is ExecutionPlanActionKind.HARDLINK:
            if self.source_relative_path is None or self.source_snapshot is None:
                raise ValueError("HARDLINK 计划必须绑定源相对路径与文件快照")
            if self.source_snapshot.size != self.length:
                raise ValueError("HARDLINK 计划长度必须与源快照一致")
        elif self.source_relative_path is not None or self.source_snapshot is not None:
            raise ValueError("非 HARDLINK 计划不能绑定源文件")


@dataclass(frozen=True, slots=True)
class ExecutionPlanSnapshot:
    task_id: str
    task_version: int
    task_unit_id: str
    execution_gate_id: str
    execution_gate_digest: str
    preflight_snapshot_id: str
    review_revision_id: str
    candidate_id: str
    source_inventory_digest: str
    metainfo_digest: str
    verification_level: VerificationLevel
    client_check_required: bool
    source_root: str
    target_root: str
    target_device: int
    actions: tuple[ExecutionPlanAction, ...]
    create_directories: tuple[str, ...]
    estimated_download_bytes_upper_bound: int
    blocked_reasons: tuple[ExecutionPlanBlockReason, ...]
    created_at: datetime
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION
    plan_digest: str = ""

    def __post_init__(self) -> None:
        if self.task_version < 1:
            raise ValueError("execution plan 必须绑定有效 task version")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("execution plan 创建时间必须带时区")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if self.verification_level not in {
            VerificationLevel.FULL_VERIFIED,
            VerificationLevel.CLIENT_CHECK_REQUIRED,
        }:
            raise ValueError("execution plan 只能绑定可进入执行准备阶段的验证等级")
        if self.client_check_required != (
            self.verification_level is VerificationLevel.CLIENT_CHECK_REQUIRED
        ):
            raise ValueError("execution plan 的 client_check_required 与验证等级不一致")
        object.__setattr__(self, "source_root", _safe_root(self.source_root))
        object.__setattr__(self, "target_root", _safe_root(self.target_root))
        if self.target_device < 0:
            raise ValueError("target device 无效")
        if self.estimated_download_bytes_upper_bound < 0:
            raise ValueError("预计下载字节不能为负数")
        action_paths = tuple(item.torrent_path for item in self.actions)
        if len(set(action_paths)) != len(action_paths):
            raise ValueError("execution plan 不能包含重复 torrent path")
        if self.verification_level is VerificationLevel.FULL_VERIFIED and any(
            item.kind is ExecutionPlanActionKind.CLIENT_FETCH for item in self.actions
        ):
            raise ValueError("FULL_VERIFIED execution plan 不能包含 CLIENT_FETCH")
        directories = tuple(sorted({_safe_relative_path(item) for item in self.create_directories}))
        object.__setattr__(self, "create_directories", directories)
        reasons = tuple(sorted(set(self.blocked_reasons), key=lambda item: item.value))
        object.__setattr__(self, "blocked_reasons", reasons)

        payload = execution_plan_to_payload(self, include_digest=False)
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
            raise ValueError("execution plan digest 与证据内容不一致")
        object.__setattr__(self, "plan_digest", digest)

    @property
    def ready(self) -> bool:
        return not self.blocked_reasons


def execution_plan_to_payload(
    snapshot: ExecutionPlanSnapshot,
    *,
    include_digest: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": snapshot.schema_version,
        "task_id": snapshot.task_id,
        "task_version": snapshot.task_version,
        "task_unit_id": snapshot.task_unit_id,
        "execution_gate_id": snapshot.execution_gate_id,
        "execution_gate_digest": snapshot.execution_gate_digest,
        "preflight_snapshot_id": snapshot.preflight_snapshot_id,
        "review_revision_id": snapshot.review_revision_id,
        "candidate_id": snapshot.candidate_id,
        "source_inventory_digest": snapshot.source_inventory_digest,
        "metainfo_digest": snapshot.metainfo_digest,
        "verification_level": snapshot.verification_level.value,
        "client_check_required": snapshot.client_check_required,
        "source_root": snapshot.source_root,
        "target_root": snapshot.target_root,
        "target_device": snapshot.target_device,
        "actions": [_action_payload(item) for item in snapshot.actions],
        "create_directories": list(snapshot.create_directories),
        "estimated_download_bytes_upper_bound": snapshot.estimated_download_bytes_upper_bound,
        "blocked_reasons": [item.value for item in snapshot.blocked_reasons],
        "ready": snapshot.ready,
        "execution_allowed": False,
        "side_effects_started": False,
        "created_at": snapshot.created_at.isoformat(),
    }
    if include_digest:
        payload["plan_digest"] = snapshot.plan_digest
    return payload


def _action_payload(action: ExecutionPlanAction) -> dict[str, object]:
    snapshot = action.source_snapshot
    return {
        "torrent_path": action.torrent_path,
        "kind": action.kind.value,
        "length": action.length,
        "source_relative_path": action.source_relative_path,
        "source_snapshot": (
            {
                "device": snapshot.device,
                "inode": snapshot.inode,
                "size": snapshot.size,
                "mtime_ns": snapshot.mtime_ns,
                "file_type": snapshot.file_type,
            }
            if snapshot is not None
            else None
        ),
    }


def _safe_root(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if normalized == ".":
        return normalized
    return _safe_relative_path(normalized)


def _safe_relative_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    has_windows_drive = len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith("/")
        or has_windows_drive
    ):
        raise ValueError("execution plan 路径必须是安全的 POSIX 相对路径")
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("execution plan 路径包含不安全路径段")
    return "/".join(parts)
