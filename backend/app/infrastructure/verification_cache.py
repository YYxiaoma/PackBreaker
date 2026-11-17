from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
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

    def __init__(self, *, max_entries: int = 32) -> None:
        if max_entries < 1:
            raise ValueError("校验缓存条目上限必须大于 0")
        self._max_entries = max_entries
        # 有界 LRU：校验结果可能包含整部影片的分片证据，必须限制常驻内存。
        self._entries: OrderedDict[str, VerificationCacheEntry[T]] = OrderedDict()
        self._lock = Lock()

    def put(self, key: VerificationCacheKey, result: T) -> None:
        with self._lock:
            self._entries[key.digest] = VerificationCacheEntry(key, result)
            self._entries.move_to_end(key.digest)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

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
        with self._lock:
            entry = self._entries.get(key.digest)
            if entry is None or entry.key != key:
                return None
            self._entries.move_to_end(key.digest)
            result = entry.result
        # 快照复查涉及文件系统 IO，放在锁外执行。
        if not mapping_snapshots_current(mappings):
            return None
        return result


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
