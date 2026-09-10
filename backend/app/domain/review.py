from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from backend.app.domain.file_mapping import AutoMappingDecision
from backend.app.domain.verification import VerificationLevel

REVIEW_VERIFICATION_SCHEMA_VERSION = "packbreaker-review-verification-v1"


@dataclass(frozen=True, slots=True)
class ManualReviewMapping:
    torrent_path: str
    source_relative_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "torrent_path", _safe_relative_path(self.torrent_path))
        object.__setattr__(
            self,
            "source_relative_path",
            _safe_relative_path(self.source_relative_path),
        )


@dataclass(frozen=True, slots=True)
class ReviewState:
    approved_candidate_id: str | None = None
    rejected_candidate_ids: tuple[str, ...] = ()
    manual_mappings: tuple[ManualReviewMapping, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        approved = self.approved_candidate_id.strip() if self.approved_candidate_id else None
        rejected = tuple(
            sorted({item.strip() for item in self.rejected_candidate_ids if item.strip()})
        )
        note = self.note.strip() if self.note and self.note.strip() else None
        object.__setattr__(self, "approved_candidate_id", approved)
        object.__setattr__(self, "rejected_candidate_ids", rejected)
        object.__setattr__(self, "note", note)
        if approved is not None and approved in rejected:
            raise ValueError("批准候选不能同时出现在拒绝集合中")
        if self.manual_mappings and approved is None:
            raise ValueError("人工映射必须绑定已批准候选")
        if approved is None and not rejected and not self.manual_mappings and note is None:
            raise ValueError("审核 revision 不能是空操作")
        torrent_paths = tuple(item.torrent_path for item in self.manual_mappings)
        source_paths = tuple(item.source_relative_path for item in self.manual_mappings)
        if len(set(torrent_paths)) != len(torrent_paths):
            raise ValueError("同一 torrent path 只能提交一条人工映射")
        if len(set(source_paths)) != len(source_paths):
            raise ValueError("同一源文件不能在一次审核中映射到多个 torrent path")


@dataclass(frozen=True, slots=True)
class ReviewVerificationSnapshot:
    task_id: str
    task_unit_id: str
    review_revision_id: str
    review_version: int
    preflight_snapshot_id: str
    candidate_id: str
    source_inventory_digest: str
    metainfo_digest: str
    verification_level: VerificationLevel
    mappings: tuple[AutoMappingDecision, ...]
    created_at: datetime
    schema_version: str = REVIEW_VERIFICATION_SCHEMA_VERSION
    verification_digest: str = ""

    def __post_init__(self) -> None:
        if self.review_version < 1:
            raise ValueError("review verification 必须绑定有效审核版本")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("review verification 创建时间必须带时区")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        payload = review_verification_to_payload(self, include_digest=False)
        payload.pop("created_at", None)
        digest = sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.verification_digest and self.verification_digest != digest:
            raise ValueError("review verification digest 与证据内容不一致")
        object.__setattr__(self, "verification_digest", digest)


def review_verification_to_payload(
    snapshot: ReviewVerificationSnapshot,
    *,
    include_digest: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": snapshot.schema_version,
        "task_id": snapshot.task_id,
        "task_unit_id": snapshot.task_unit_id,
        "review_revision_id": snapshot.review_revision_id,
        "review_version": snapshot.review_version,
        "preflight_snapshot_id": snapshot.preflight_snapshot_id,
        "candidate_id": snapshot.candidate_id,
        "source_inventory_digest": snapshot.source_inventory_digest,
        "metainfo_digest": snapshot.metainfo_digest,
        "verification_level": snapshot.verification_level.value,
        "mappings": [
            {
                "torrent_path": mapping.torrent_path,
                "state": mapping.state.value,
                "method": mapping.method.value,
                "source_path": mapping.source_path,
                "snapshot": (
                    {
                        "device": mapping.snapshot.device,
                        "inode": mapping.snapshot.inode,
                        "size": mapping.snapshot.size,
                        "mtime_ns": mapping.snapshot.mtime_ns,
                        "file_type": mapping.snapshot.file_type,
                    }
                    if mapping.snapshot is not None
                    else None
                ),
                "candidate_paths": list(mapping.candidate_paths),
            }
            for mapping in snapshot.mappings
        ],
        "created_at": snapshot.created_at.isoformat(),
    }
    if include_digest:
        payload["verification_digest"] = snapshot.verification_digest
    return payload


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
        raise ValueError("审核映射路径必须是安全的 POSIX 相对路径")
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("审核映射路径包含不安全路径段")
    return "/".join(parts)
