from __future__ import annotations

import unicodedata
from dataclasses import dataclass


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
