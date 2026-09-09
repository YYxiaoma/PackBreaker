import hashlib
import os
from pathlib import Path

import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.torrent import PieceLayer, TorrentFile, TorrentKind, TorrentMeta
from backend.app.domain.verification import FileMappingState, PieceStatus, VerificationLevel
from backend.app.infrastructure.piece_verifier import V2FileMapping, verify_hybrid, verify_v2_files

BLOCK = 16 * 1024


def _file(
    path: str,
    length: int,
    root: bytes | None,
    *,
    padding: bool = False,
    order: int = 0,
) -> TorrentFile:
    return TorrentFile(
        path=path,
        raw_path_hex=(path.encode().hex(),),
        length=length,
        padding=padding,
        zero_length=length == 0,
        order=order,
        pieces_root=root,
    )


def _meta(
    files: tuple[TorrentFile, ...],
    *,
    piece_length: int,
    layers: tuple[PieceLayer, ...] = (),
    kind: TorrentKind = TorrentKind.V2,
    v1_hashes: tuple[bytes, ...] = (),
) -> TorrentMeta:
    return TorrentMeta(
        torrent_kind=kind,
        v1_info_hash="00" * 20 if kind is TorrentKind.HYBRID else None,
        v2_info_hash="11" * 32,
        piece_length=piece_length,
        files=files,
        v1_piece_hashes=v1_hashes,
        v2_piece_layers=layers,
        private=False,
        source=None,
        display_name="fixture",
        metainfo_digest="22" * 32,
        info_span=(0, 0),
    )


def test_v2_small_file_merkle_root_is_verified(tmp_path: Path) -> None:
    content = b"a" * BLOCK + b"tail"
    source = tmp_path / "movie.bin"
    source.write_bytes(content)
    first = hashlib.sha256(content[:BLOCK]).digest()
    second = hashlib.sha256(content[BLOCK:]).digest()
    root = hashlib.sha256(first + second).digest()
    meta = _meta((_file("movie.bin", len(content), root),), piece_length=2 * BLOCK)

    result = verify_v2_files(
        meta,
        (V2FileMapping("movie.bin", FileMappingState.MAPPED, source),),
        read_chunk_bytes=1024,
    )

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert result.files[0].status is PieceStatus.VERIFIED
    assert result.files[0].pieces[0].status is PieceStatus.VERIFIED


def test_v2_piece_layer_handles_partial_last_piece_and_zero_leaf(tmp_path: Path) -> None:
    first_block = b"a" * BLOCK
    second_block = b"b" * BLOCK
    tail = b"z"
    content = first_block + second_block + tail
    source = tmp_path / "large.bin"
    source.write_bytes(content)

    first_piece = hashlib.sha256(
        hashlib.sha256(first_block).digest() + hashlib.sha256(second_block).digest()
    ).digest()
    second_piece = hashlib.sha256(hashlib.sha256(tail).digest() + bytes(32)).digest()
    root = hashlib.sha256(first_piece + second_piece).digest()
    layer = PieceLayer(root, (first_piece, second_piece))
    meta = _meta(
        (_file("large.bin", len(content), root),),
        piece_length=2 * BLOCK,
        layers=(layer,),
    )

    result = verify_v2_files(
        meta,
        (V2FileMapping("large.bin", FileMappingState.MAPPED, source),),
        read_chunk_bytes=4096,
    )

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert [piece.status for piece in result.files[0].pieces] == [
        PieceStatus.VERIFIED,
        PieceStatus.VERIFIED,
    ]


def test_v2_piece_layer_root_pads_missing_whole_piece_subtree(tmp_path: Path) -> None:
    piece_length = 2 * BLOCK
    piece_a = b"a" * piece_length
    piece_b = b"b" * piece_length
    tail = b"c"
    content = piece_a + piece_b + tail
    source = tmp_path / "three-pieces.bin"
    source.write_bytes(content)

    def full_piece(value: bytes) -> bytes:
        return hashlib.sha256(
            hashlib.sha256(value[:BLOCK]).digest() + hashlib.sha256(value[BLOCK:]).digest()
        ).digest()

    first = full_piece(piece_a)
    second = full_piece(piece_b)
    third = hashlib.sha256(hashlib.sha256(tail).digest() + bytes(32)).digest()
    zero_piece = hashlib.sha256(bytes(64)).digest()
    root = hashlib.sha256(
        hashlib.sha256(first + second).digest() + hashlib.sha256(third + zero_piece).digest()
    ).digest()
    meta = _meta(
        (_file("three-pieces.bin", len(content), root),),
        piece_length=piece_length,
        layers=(PieceLayer(root, (first, second, third)),),
    )

    result = verify_v2_files(
        meta,
        (V2FileMapping("three-pieces.bin", FileMappingState.MAPPED, source),),
    )

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert len(result.files[0].pieces) == 3


def test_v2_mismatch_never_becomes_full_verified(tmp_path: Path) -> None:
    source = tmp_path / "movie.bin"
    source.write_bytes(b"local")
    expected_root = hashlib.sha256(b"other").digest()
    meta = _meta((_file("movie.bin", 5, expected_root),), piece_length=BLOCK)

    result = verify_v2_files(
        meta,
        (V2FileMapping("movie.bin", FileMappingState.MAPPED, source),),
    )

    assert result.level is VerificationLevel.CLIENT_CHECK_REQUIRED
    assert result.files[0].status is PieceStatus.MISMATCH


def test_v2_missing_and_ambiguous_have_distinct_safety_levels() -> None:
    root = hashlib.sha256(b"x").digest()
    meta = _meta((_file("movie.bin", 1, root),), piece_length=BLOCK)

    missing = verify_v2_files(meta, (V2FileMapping("movie.bin", FileMappingState.MISSING),))
    ambiguous = verify_v2_files(
        meta,
        (V2FileMapping("movie.bin", FileMappingState.AMBIGUOUS),),
    )

    assert missing.level is VerificationLevel.CLIENT_CHECK_REQUIRED
    assert ambiguous.level is VerificationLevel.BLOCKED


def test_v2_padding_is_verified_from_virtual_zero_bytes() -> None:
    root = hashlib.sha256(bytes(7)).digest()
    meta = _meta((_file(".pad/7", 7, root, padding=True),), piece_length=BLOCK)

    result = verify_v2_files(meta, ())

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert result.mappings[0].state is FileMappingState.PADDING


def test_v2_zero_length_file_needs_no_mapping() -> None:
    meta = _meta((_file("empty.txt", 0, None),), piece_length=BLOCK)

    result = verify_v2_files(meta, ())

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert result.mappings[0].state is FileMappingState.ZERO_LENGTH


def test_v2_source_change_after_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "movie.bin"
    source.write_bytes(b"abcd")
    root = hashlib.sha256(b"abcd").digest()
    meta = _meta((_file("movie.bin", 4, root),), piece_length=BLOCK)
    original_read = os.read
    changed = False

    def read_and_touch(fd: int, size: int) -> bytes:
        nonlocal changed
        value = original_read(fd, size)
        if not changed:
            changed = True
            current = source.stat()
            os.utime(source, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
        return value

    monkeypatch.setattr(os, "read", read_and_touch)
    with pytest.raises(DomainViolation) as failure:
        verify_v2_files(
            meta,
            (V2FileMapping("movie.bin", FileMappingState.MAPPED, source),),
        )
    assert failure.value.code is ErrorCode.SOURCE_CHANGED


def test_hybrid_requires_both_v1_and_v2_to_verify(tmp_path: Path) -> None:
    content = b"abc"
    source = tmp_path / "movie.bin"
    source.write_bytes(content)
    root = hashlib.sha256(content).digest()
    meta = _meta(
        (_file("movie.bin", len(content), root),),
        piece_length=BLOCK,
        kind=TorrentKind.HYBRID,
        v1_hashes=(hashlib.sha1(content).digest(),),
    )

    result = verify_hybrid(
        meta,
        (V2FileMapping("movie.bin", FileMappingState.MAPPED, source),),
    )

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert result.v1.level is VerificationLevel.FULL_VERIFIED
    assert result.v2.level is VerificationLevel.FULL_VERIFIED


def test_hybrid_v2_mismatch_downgrades_whole_result(tmp_path: Path) -> None:
    content = b"abc"
    source = tmp_path / "movie.bin"
    source.write_bytes(content)
    meta = _meta(
        (_file("movie.bin", len(content), hashlib.sha256(b"xyz").digest()),),
        piece_length=BLOCK,
        kind=TorrentKind.HYBRID,
        v1_hashes=(hashlib.sha1(content).digest(),),
    )

    result = verify_hybrid(
        meta,
        (V2FileMapping("movie.bin", FileMappingState.MAPPED, source),),
    )

    assert result.v1.level is VerificationLevel.FULL_VERIFIED
    assert result.v2.level is VerificationLevel.CLIENT_CHECK_REQUIRED
    assert result.level is VerificationLevel.CLIENT_CHECK_REQUIRED
