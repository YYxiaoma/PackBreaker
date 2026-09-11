from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.torrent import TorrentKind, TorrentMeta
from backend.app.domain.verification import (
    FileMappingEvidence,
    FileMappingState,
    FileSnapshot,
    HybridVerificationResult,
    PieceEvidence,
    PieceStatus,
    V1VerificationResult,
    V2FileEvidence,
    V2VerificationResult,
    VerificationLevel,
)
from backend.app.infrastructure.torrent_merkle import (
    V2_BLOCK_SIZE,
    merkle_root,
    piece_layer_height,
    root_from_piece_layer,
)


@dataclass(frozen=True, slots=True)
class V1FileMapping:
    torrent_path: str
    state: FileMappingState
    source_path: Path | None = None


V2FileMapping = V1FileMapping
_CANCEL_CHECK_PIECE_INTERVAL = 64


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
    cancel_check: Callable[[], None] | None = None,
) -> V1VerificationResult:
    """只读验证 v1 逻辑字节流；不会创建或修改任何文件。"""

    if meta.torrent_kind not in {TorrentKind.V1, TorrentKind.HYBRID}:
        raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "候选不包含 v1 piece 元数据")
    if read_chunk_bytes <= 0:
        raise ValueError("read_chunk_bytes 必须大于 0")
    _check_cancel(cancel_check)

    mapping_by_path = _mapping_index(mappings)
    evidence: list[FileMappingEvidence] = []
    spans: list[_ReadableSpan] = []
    blocked = False
    unavailable = False

    for file_index, torrent_file in enumerate(meta.files):
        if file_index % 128 == 0:
            _check_cancel(cancel_check)
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
        if torrent_file.zero_length:
            evidence.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.ZERO_LENGTH,
                    source_path=None,
                    snapshot=None,
                )
            )
            spans.append(_ReadableSpan(torrent_file.path, 0, None, None, False))
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

    pieces = _verify_stream(meta, tuple(spans), read_chunk_bytes, cancel_check)
    _check_cancel(cancel_check)
    _recheck_snapshots(tuple(spans))

    if blocked:
        level = VerificationLevel.BLOCKED
    elif unavailable or any(piece.status is not PieceStatus.VERIFIED for piece in pieces):
        level = VerificationLevel.CLIENT_CHECK_REQUIRED
    else:
        level = VerificationLevel.FULL_VERIFIED
    return V1VerificationResult(level=level, mappings=tuple(evidence), pieces=pieces)


def verify_v2_files(
    meta: TorrentMeta,
    mappings: tuple[V2FileMapping, ...],
    *,
    read_chunk_bytes: int = 1024 * 1024,
    cancel_check: Callable[[], None] | None = None,
) -> V2VerificationResult:
    """按 BEP 52 对每个 v2 文件独立执行只读 Merkle 验证。"""

    if meta.torrent_kind not in {TorrentKind.V2, TorrentKind.HYBRID}:
        raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "候选不包含 v2 Merkle 元数据")
    if read_chunk_bytes <= 0:
        raise ValueError("read_chunk_bytes 必须大于 0")
    _check_cancel(cancel_check)

    mapping_by_path = _mapping_index(mappings)
    layer_by_root = {layer.pieces_root: layer.hashes for layer in meta.v2_piece_layers}
    mapping_evidence: list[FileMappingEvidence] = []
    file_evidence: list[V2FileEvidence] = []
    snapshots: list[tuple[Path, FileSnapshot]] = []
    blocked = False
    unavailable = False

    for file_index, torrent_file in enumerate(meta.files):
        if file_index % 128 == 0:
            _check_cancel(cancel_check)
        if torrent_file.zero_length:
            mapping_evidence.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.ZERO_LENGTH,
                    source_path=None,
                    snapshot=None,
                )
            )
            file_evidence.append(V2FileEvidence(torrent_file.path, PieceStatus.VERIFIED, ()))
            continue

        expected_root = torrent_file.pieces_root
        if expected_root is None:
            raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "非空 v2 file 缺少 pieces root")

        if torrent_file.padding:
            mapping_evidence.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.PADDING,
                    source_path=None,
                    snapshot=None,
                )
            )
            result = _verify_v2_file_bytes(
                torrent_path=torrent_file.path,
                source_path=None,
                virtual_zero=True,
                file_length=torrent_file.length,
                piece_length=meta.piece_length,
                expected_root=expected_root,
                expected_layer=layer_by_root.get(expected_root),
                read_chunk_bytes=read_chunk_bytes,
                cancel_check=cancel_check,
            )
            file_evidence.append(result)
            if result.status is not PieceStatus.VERIFIED:
                unavailable = True
            continue

        mapping = mapping_by_path.get(torrent_file.path)
        if mapping is None or mapping.state is FileMappingState.MISSING:
            mapping_evidence.append(
                FileMappingEvidence(torrent_file.path, FileMappingState.MISSING, None, None)
            )
            file_evidence.append(V2FileEvidence(torrent_file.path, PieceStatus.UNAVAILABLE, ()))
            unavailable = True
            continue
        if mapping.state is FileMappingState.AMBIGUOUS:
            mapping_evidence.append(
                FileMappingEvidence(torrent_file.path, FileMappingState.AMBIGUOUS, None, None)
            )
            file_evidence.append(V2FileEvidence(torrent_file.path, PieceStatus.UNAVAILABLE, ()))
            blocked = True
            continue
        if mapping.state is not FileMappingState.MAPPED or mapping.source_path is None:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射状态与源路径不一致")

        snapshot = _snapshot(mapping.source_path)
        if snapshot.size != torrent_file.length:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射源文件长度与 torrent 不一致")
        mapping_evidence.append(
            FileMappingEvidence(
                torrent_file.path,
                FileMappingState.MAPPED,
                str(mapping.source_path),
                snapshot,
            )
        )
        snapshots.append((mapping.source_path, snapshot))
        result = _verify_v2_file_bytes(
            torrent_path=torrent_file.path,
            source_path=mapping.source_path,
            virtual_zero=False,
            file_length=torrent_file.length,
            piece_length=meta.piece_length,
            expected_root=expected_root,
            expected_layer=layer_by_root.get(expected_root),
            read_chunk_bytes=read_chunk_bytes,
            expected_snapshot=snapshot,
            cancel_check=cancel_check,
        )
        file_evidence.append(result)
        if result.status is not PieceStatus.VERIFIED:
            unavailable = True

    _check_cancel(cancel_check)
    _recheck_path_snapshots(tuple(snapshots))
    if blocked:
        level = VerificationLevel.BLOCKED
    elif unavailable:
        level = VerificationLevel.CLIENT_CHECK_REQUIRED
    else:
        level = VerificationLevel.FULL_VERIFIED
    return V2VerificationResult(level, tuple(mapping_evidence), tuple(file_evidence))


def verify_hybrid(
    meta: TorrentMeta,
    mappings: tuple[V1FileMapping, ...],
    *,
    read_chunk_bytes: int = 1024 * 1024,
    cancel_check: Callable[[], None] | None = None,
) -> HybridVerificationResult:
    """hybrid 必须同时满足 v1 逻辑流和 v2 文件 Merkle 证据。"""

    if meta.torrent_kind is not TorrentKind.HYBRID:
        raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "候选不是 hybrid torrent")
    v1 = verify_v1_pieces(
        meta,
        mappings,
        read_chunk_bytes=read_chunk_bytes,
        cancel_check=cancel_check,
    )
    _check_cancel(cancel_check)
    v2 = verify_v2_files(
        meta,
        mappings,
        read_chunk_bytes=read_chunk_bytes,
        cancel_check=cancel_check,
    )
    _assert_same_mapping_snapshots(v1.mappings, v2.mappings)

    if VerificationLevel.BLOCKED in {v1.level, v2.level}:
        level = VerificationLevel.BLOCKED
    elif (
        v1.level is VerificationLevel.FULL_VERIFIED and v2.level is VerificationLevel.FULL_VERIFIED
    ):
        level = VerificationLevel.FULL_VERIFIED
    else:
        level = VerificationLevel.CLIENT_CHECK_REQUIRED
    return HybridVerificationResult(level, v1, v2)


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
    cancel_check: Callable[[], None] | None,
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
        if piece_index % _CANCEL_CHECK_PIECE_INTERVAL == 0:
            _check_cancel(cancel_check)
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
                if span.snapshot is None:
                    raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射源缺少文件快照")
                _hash_file_range(
                    digest,
                    span.source_path,
                    span.snapshot,
                    span_offset,
                    take,
                    read_chunk_bytes,
                )

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
    snapshot: FileSnapshot,
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
        _assert_fd_snapshot(fd, snapshot)
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


def _recheck_path_snapshots(snapshots: tuple[tuple[Path, FileSnapshot], ...]) -> None:
    checked: set[Path] = set()
    for path, snapshot in snapshots:
        if path in checked:
            continue
        checked.add(path)
        if _snapshot(path) != snapshot:
            raise DomainViolation(ErrorCode.SOURCE_CHANGED, "Merkle 验证期间源文件发生变化")


def _verify_v2_file_bytes(
    *,
    torrent_path: str,
    source_path: Path | None,
    virtual_zero: bool,
    file_length: int,
    piece_length: int,
    expected_root: bytes,
    expected_layer: tuple[bytes, ...] | None,
    read_chunk_bytes: int,
    expected_snapshot: FileSnapshot | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> V2FileEvidence:
    piece_count = (file_length + piece_length - 1) // piece_length
    if piece_count > 1:
        if expected_layer is None:
            raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "大文件 v2 piece layer 缺失")
        try:
            layer_root = root_from_piece_layer(
                expected_layer,
                piece_length=piece_length,
                file_length=file_length,
            )
        except ValueError as exc:
            raise DomainViolation(ErrorCode.TORRENT_META_INVALID, str(exc)) from exc
        if not hmac.compare_digest(layer_root, expected_root):
            raise DomainViolation(
                ErrorCode.TORRENT_META_INVALID, "v2 piece layer 与 pieces root 不一致"
            )
    elif expected_layer is not None:
        raise DomainViolation(ErrorCode.TORRENT_META_INVALID, "小文件不应包含 v2 piece layer")

    computed = _compute_v2_piece_hashes(
        source_path=source_path,
        virtual_zero=virtual_zero,
        file_length=file_length,
        piece_length=piece_length,
        read_chunk_bytes=read_chunk_bytes,
        expected_snapshot=expected_snapshot,
        cancel_check=cancel_check,
    )
    piece_statuses: list[PieceEvidence] = []
    if piece_count > 1:
        assert expected_layer is not None
        for index, value in enumerate(computed):
            status = (
                PieceStatus.VERIFIED
                if hmac.compare_digest(value, expected_layer[index])
                else PieceStatus.MISMATCH
            )
            piece_statuses.append(PieceEvidence(index, status, (torrent_path,)))
        status = (
            PieceStatus.VERIFIED
            if all(item.status is PieceStatus.VERIFIED for item in piece_statuses)
            else PieceStatus.MISMATCH
        )
    else:
        root = computed[0]
        status = (
            PieceStatus.VERIFIED
            if hmac.compare_digest(root, expected_root)
            else PieceStatus.MISMATCH
        )
        piece_statuses.append(PieceEvidence(0, status, (torrent_path,)))
    return V2FileEvidence(torrent_path, status, tuple(piece_statuses))


def _compute_v2_piece_hashes(
    *,
    source_path: Path | None,
    virtual_zero: bool,
    file_length: int,
    piece_length: int,
    read_chunk_bytes: int,
    expected_snapshot: FileSnapshot | None,
    cancel_check: Callable[[], None] | None,
) -> tuple[bytes, ...]:
    blocks_per_piece = 1 << piece_layer_height(piece_length)
    fd: int | None = None
    if not virtual_zero:
        if source_path is None:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "v2 验证缺少源文件")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(source_path, flags)
        except OSError as exc:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法安全打开映射源文件") from exc
        if expected_snapshot is None:
            os.close(fd)
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "v2 映射源缺少文件快照")
        _assert_fd_snapshot(fd, expected_snapshot)

    pieces: list[bytes] = []
    remaining = file_length
    try:
        while remaining > 0:
            if len(pieces) % _CANCEL_CHECK_PIECE_INTERVAL == 0:
                _check_cancel(cancel_check)
            bytes_in_piece = min(piece_length, remaining)
            block_hashes: list[bytes] = []
            piece_remaining = bytes_in_piece
            while piece_remaining > 0:
                block_length = min(V2_BLOCK_SIZE, piece_remaining)
                if virtual_zero:
                    block = bytes(block_length)
                else:
                    assert fd is not None
                    block = _read_exact(fd, block_length, read_chunk_bytes)
                block_hashes.append(hashlib.sha256(block).digest())
                piece_remaining -= block_length
            if remaining <= piece_length and len(pieces) == 0 and file_length <= piece_length:
                pieces.append(merkle_root(tuple(block_hashes)))
            else:
                pieces.append(merkle_root(tuple(block_hashes), target_count=blocks_per_piece))
            remaining -= bytes_in_piece
    finally:
        if fd is not None:
            os.close(fd)
    return tuple(pieces)


def _read_exact(fd: int, length: int, chunk_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(fd, min(remaining, chunk_size))
        if not chunk:
            raise DomainViolation(ErrorCode.SOURCE_CHANGED, "Merkle 验证期间源文件提前结束")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _assert_same_mapping_snapshots(
    first: tuple[FileMappingEvidence, ...],
    second: tuple[FileMappingEvidence, ...],
) -> None:
    left = {item.torrent_path: (item.state, item.source_path, item.snapshot) for item in first}
    right = {item.torrent_path: (item.state, item.source_path, item.snapshot) for item in second}
    if left != right:
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "hybrid 双重验证期间映射快照发生变化")


def _assert_fd_snapshot(fd: int, expected: FileSnapshot) -> None:
    current = os.fstat(fd)
    observed = FileSnapshot(
        device=current.st_dev,
        inode=current.st_ino,
        size=current.st_size,
        mtime_ns=current.st_mtime_ns,
    )
    if observed != expected:
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "打开的源文件与验证快照不一致")


def _check_cancel(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check is not None:
        cancel_check()
