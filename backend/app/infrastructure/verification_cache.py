from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Generic, TypeVar

from backend.app.domain.errors import DomainViolation
from backend.app.domain.file_mapping import AutoMappingDecision
from backend.app.domain.torrent import TorrentMeta
from backend.app.domain.verification import FileMappingState
from backend.app.infrastructure.source_inventory import current_file_snapshot

T = TypeVar("T")

DEFAULT_ALGORITHM_VERSION = "packbreaker-piece-verifier-v2"
DEFAULT_READ_POLICY = "nofollow-fstat-stream-v1"
_CACHE_KEY_VERSION = "packbreaker-verification-cache-v1"


@dataclass(frozen=True, slots=True)
class VerificationCacheKey:
    digest: str
    algorithm_version: str
    read_policy: str


@dataclass(frozen=True, slots=True)
class VerificationCacheEntry(Generic[T]):
    key: VerificationCacheKey
    result: T


class VerificationResultCache(Generic[T]):
    """进程内结果缓存；命中仍必须复查全部映射源快照。"""

    def __init__(self) -> None:
        self._entries: dict[str, VerificationCacheEntry[T]] = {}

    def put(self, key: VerificationCacheKey, result: T) -> None:
        self._entries[key.digest] = VerificationCacheEntry(key, result)

    def get_if_current(
        self,
        meta: TorrentMeta,
        mappings: tuple[AutoMappingDecision, ...],
        *,
        algorithm_version: str = DEFAULT_ALGORITHM_VERSION,
        read_policy: str = DEFAULT_READ_POLICY,
    ) -> T | None:
        key = build_verification_cache_key(
            meta,
            mappings,
            algorithm_version=algorithm_version,
            read_policy=read_policy,
        )
        entry = self._entries.get(key.digest)
        if entry is None or entry.key != key:
            return None
        if not mapping_snapshots_current(mappings):
            return None
        return entry.result


def build_verification_cache_key(
    meta: TorrentMeta,
    mappings: tuple[AutoMappingDecision, ...],
    *,
    algorithm_version: str = DEFAULT_ALGORITHM_VERSION,
    read_policy: str = DEFAULT_READ_POLICY,
) -> VerificationCacheKey:
    if not algorithm_version or not read_policy:
        raise ValueError("缓存算法版本和读取策略不能为空")
    expected_paths = tuple(file.path for file in meta.files)
    actual_paths = tuple(mapping.torrent_path for mapping in mappings)
    if actual_paths != expected_paths:
        raise ValueError("缓存映射必须按 torrent 文件顺序完整覆盖")

    parts: list[str] = [meta.metainfo_digest, algorithm_version, read_policy]
    for mapping in mappings:
        parts.extend(
            [
                mapping.torrent_path,
                mapping.state.value,
                mapping.method.value,
                mapping.source_path or "",
                str(len(mapping.candidate_paths)),
                *mapping.candidate_paths,
            ]
        )
        snapshot = mapping.snapshot
        if snapshot is None:
            parts.extend(["", "", "", "", ""])
        else:
            parts.extend(
                [
                    str(snapshot.device),
                    str(snapshot.inode),
                    str(snapshot.size),
                    str(snapshot.mtime_ns),
                    snapshot.file_type,
                ]
            )
    digest = _length_prefixed_digest(parts)
    return VerificationCacheKey(digest, algorithm_version, read_policy)


def mapping_snapshots_current(mappings: tuple[AutoMappingDecision, ...]) -> bool:
    for mapping in mappings:
        if mapping.state is not FileMappingState.MAPPED:
            continue
        if mapping.source_path is None or mapping.snapshot is None:
            return False
        try:
            current = current_file_snapshot(Path(mapping.source_path))
        except DomainViolation:
            return False
        if current != mapping.snapshot:
            return False
    return True


def _length_prefixed_digest(parts: list[str]) -> str:
    payload = bytearray(_CACHE_KEY_VERSION.encode("utf-8"))
    for value in parts:
        encoded = value.encode("utf-8")
        payload.extend(len(encoded).to_bytes(8, byteorder="big", signed=False))
        payload.extend(encoded)
    return sha256(payload).hexdigest()
