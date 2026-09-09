from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.torrent import TorrentKind, TorrentMeta
from backend.app.domain.verification import (
    FileMappingEvidence,
    FileMappingState,
    FileSnapshot,
    PieceEvidence,
    PieceStatus,
    V1VerificationResult,
    VerificationLevel,
)


@dataclass(frozen=True, slots=True)
class V1FileMapping:
    torrent_path: str
    state: FileMappingState
    source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _ReadableSpan:
    torrent_path: str
    length: int
    source_path: Path | None
    snapshot: FileSnapshot | None
    padding: bool


def verify_v1_pieces(
    meta: TorrentMeta,
    mappings: tuple[V1FileMapping, ...],
    *,
    read_chunk_bytes: int = 1024 * 1024,
) -> V1VerificationResult:
    """只读验证 v1 逻辑字节流；不会创建或修改任何文件。"""

    if meta.torrent_kind not in {TorrentKind.V1, TorrentKind.HYBRID}:
        raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "候选不包含 v1 piece 元数据")
    if read_chunk_bytes <= 0:
        raise ValueError("read_chunk_bytes 必须大于 0")

    mapping_by_path = _mapping_index(mappings)
    evidence: list[FileMappingEvidence] = []
    spans: list[_ReadableSpan] = []
    blocked = False
    unavailable = False

    for torrent_file in meta.files:
        if torrent_file.padding:
            evidence.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.PADDING,
                    source_path=None,
                    snapshot=None,
                )
            )
            spans.append(
                _ReadableSpan(
                    torrent_path=torrent_file.path,
                    length=torrent_file.length,
                    source_path=None,
                    snapshot=None,
                    padding=True,
                )
            )
            continue

        mapping = mapping_by_path.get(torrent_file.path)
        if mapping is None or mapping.state is FileMappingState.MISSING:
            evidence.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.MISSING,
                    source_path=None,
                    snapshot=None,
                )
            )
            spans.append(_ReadableSpan(torrent_file.path, torrent_file.length, None, None, False))
            unavailable = True
            continue
        if mapping.state is FileMappingState.AMBIGUOUS:
            evidence.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.AMBIGUOUS,
                    source_path=None,
                    snapshot=None,
                )
            )
            spans.append(_ReadableSpan(torrent_file.path, torrent_file.length, None, None, False))
            blocked = True
            continue
        if mapping.state is not FileMappingState.MAPPED or mapping.source_path is None:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射状态与源路径不一致")

        snapshot = _snapshot(mapping.source_path)
        if snapshot.size != torrent_file.length:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射源文件长度与 torrent 不一致")
        source_text = str(mapping.source_path)
        evidence.append(
            FileMappingEvidence(
                torrent_path=torrent_file.path,
                state=FileMappingState.MAPPED,
                source_path=source_text,
                snapshot=snapshot,
            )
        )
        spans.append(
            _ReadableSpan(
                torrent_file.path,
                torrent_file.length,
                mapping.source_path,
                snapshot,
                False,
            )
        )

    pieces = _verify_stream(meta, tuple(spans), read_chunk_bytes)
    _recheck_snapshots(tuple(spans))

    if blocked:
        level = VerificationLevel.BLOCKED
    elif unavailable or any(piece.status is not PieceStatus.VERIFIED for piece in pieces):
        level = VerificationLevel.CLIENT_CHECK_REQUIRED
    else:
        level = VerificationLevel.FULL_VERIFIED
    return V1VerificationResult(level=level, mappings=tuple(evidence), pieces=pieces)


def _mapping_index(mappings: tuple[V1FileMapping, ...]) -> dict[str, V1FileMapping]:
    indexed: dict[str, V1FileMapping] = {}
    for mapping in mappings:
        if mapping.torrent_path in indexed:
            raise DomainViolation(ErrorCode.MAPPING_AMBIGUOUS, "同一 torrent 文件存在重复映射")
        indexed[mapping.torrent_path] = mapping
    return indexed


def _snapshot(path: Path) -> FileSnapshot:
    try:
        result = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取映射源文件状态") from exc
    if not stat.S_ISREG(result.st_mode):
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "映射源必须是普通文件且不能是符号链接"
        )
    return FileSnapshot(
        device=result.st_dev,
        inode=result.st_ino,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
    )


def _verify_stream(
    meta: TorrentMeta,
    spans: tuple[_ReadableSpan, ...],
    read_chunk_bytes: int,
) -> tuple[PieceEvidence, ...]:
    total_length = sum(span.length for span in spans)
    expected_piece_count = (
        (total_length + meta.piece_length - 1) // meta.piece_length if total_length else 0
    )
    if expected_piece_count != len(meta.v1_piece_hashes):
        raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "v1 pieces 数量与声明字节范围不一致")

    pieces: list[PieceEvidence] = []
    span_index = 0
    span_offset = 0
    for piece_index, expected_hash in enumerate(meta.v1_piece_hashes):
        remaining = min(meta.piece_length, total_length - piece_index * meta.piece_length)
        digest = hashlib.sha1()
        available = True
        covered: list[str] = []

        while remaining > 0:
            span = spans[span_index]
            take = min(remaining, span.length - span_offset)
            if take == 0:
                span_index += 1
                span_offset = 0
                continue
            if not covered or covered[-1] != span.torrent_path:
                covered.append(span.torrent_path)

            if span.padding:
                _hash_zeros(digest, take, read_chunk_bytes)
            elif span.source_path is None:
                available = False
            elif available:
                _hash_file_range(digest, span.source_path, span_offset, take, read_chunk_bytes)

            span_offset += take
            remaining -= take
            if span_offset == span.length:
                span_index += 1
                span_offset = 0

        if not available:
            status = PieceStatus.UNAVAILABLE
        elif hmac.compare_digest(digest.digest(), expected_hash):
            status = PieceStatus.VERIFIED
        else:
            status = PieceStatus.MISMATCH
        pieces.append(PieceEvidence(piece_index, status, tuple(covered)))
    return tuple(pieces)


def _hash_file_range(
    digest: object,
    path: Path,
    offset: int,
    length: int,
    chunk_size: int,
) -> None:
    hasher = digest
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法安全打开映射源文件") from exc
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        remaining = length
        while remaining:
            chunk = os.read(fd, min(remaining, chunk_size))
            if not chunk:
                raise DomainViolation(ErrorCode.SOURCE_CHANGED, "读取期间源文件提前结束")
            hasher.update(chunk)  # type: ignore[attr-defined]
            remaining -= len(chunk)
    finally:
        os.close(fd)


def _hash_zeros(digest: object, length: int, chunk_size: int) -> None:
    zero_chunk = bytes(min(length, chunk_size))
    remaining = length
    while remaining:
        take = min(remaining, len(zero_chunk))
        digest.update(zero_chunk[:take])  # type: ignore[attr-defined]
        remaining -= take


def _recheck_snapshots(spans: tuple[_ReadableSpan, ...]) -> None:
    checked: set[Path] = set()
    for span in spans:
        if span.source_path is None or span.snapshot is None or span.source_path in checked:
            continue
        checked.add(span.source_path)
        if _snapshot(span.source_path) != span.snapshot:
            raise DomainViolation(ErrorCode.SOURCE_CHANGED, "piece 验证期间源文件发生变化")
