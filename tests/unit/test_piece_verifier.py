import hashlib
import os
from pathlib import Path

import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.torrent import TorrentFile, TorrentKind, TorrentMeta
from backend.app.domain.verification import FileMappingState, PieceStatus, VerificationLevel
from backend.app.infrastructure.piece_verifier import V1FileMapping, verify_v1_pieces


def _meta(files: tuple[TorrentFile, ...], content: bytes, piece_length: int = 4) -> TorrentMeta:
    hashes = tuple(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    return TorrentMeta(
        torrent_kind=TorrentKind.V1,
        v1_info_hash="00" * 20,
        v2_info_hash=None,
        piece_length=piece_length,
        files=files,
        v1_piece_hashes=hashes,
        v2_piece_layers=(),
        private=False,
        source=None,
        display_name="fixture",
        metainfo_digest="11" * 32,
        info_span=(0, 0),
    )


def _file(path: str, length: int, order: int, *, padding: bool = False) -> TorrentFile:
    return TorrentFile(path, (path.encode().hex(),), length, padding, length == 0, order)


def test_v1_verification_streams_across_file_boundary(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"abc")
    second.write_bytes(b"defgh")
    meta = _meta((_file("a.bin", 3, 0), _file("b.bin", 5, 1)), b"abcdefgh")

    result = verify_v1_pieces(
        meta,
        (
            V1FileMapping("a.bin", FileMappingState.MAPPED, first),
            V1FileMapping("b.bin", FileMappingState.MAPPED, second),
        ),
        read_chunk_bytes=2,
    )

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert [piece.status for piece in result.pieces] == [PieceStatus.VERIFIED, PieceStatus.VERIFIED]
    assert result.pieces[0].covered_files == ("a.bin", "b.bin")


def test_padding_is_hashed_as_virtual_zero_bytes(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    files = (_file("media.bin", 4, 0), _file(".pad/4", 4, 1, padding=True))
    meta = _meta(files, b"abcd" + bytes(4))

    result = verify_v1_pieces(
        meta,
        (V1FileMapping("media.bin", FileMappingState.MAPPED, media),),
    )

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert result.mappings[1].state is FileMappingState.PADDING


def test_zero_length_v1_file_needs_no_source_mapping() -> None:
    meta = _meta((_file("empty.txt", 0, 0),), b"")

    result = verify_v1_pieces(meta, ())

    assert result.level is VerificationLevel.FULL_VERIFIED
    assert result.mappings[0].state is FileMappingState.ZERO_LENGTH


def test_missing_range_downgrades_to_client_check_required(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    files = (_file("media.bin", 4, 0), _file("extra.nfo", 4, 1))
    meta = _meta(files, b"abcdefgh")

    result = verify_v1_pieces(
        meta,
        (
            V1FileMapping("media.bin", FileMappingState.MAPPED, media),
            V1FileMapping("extra.nfo", FileMappingState.MISSING),
        ),
    )

    assert result.level is VerificationLevel.CLIENT_CHECK_REQUIRED
    assert result.pieces[1].status is PieceStatus.UNAVAILABLE


def test_ambiguous_range_is_blocked(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    files = (_file("media.bin", 4, 0), _file("other.bin", 4, 1))
    meta = _meta(files, b"abcdefgh")

    result = verify_v1_pieces(
        meta,
        (
            V1FileMapping("media.bin", FileMappingState.MAPPED, media),
            V1FileMapping("other.bin", FileMappingState.AMBIGUOUS),
        ),
    )

    assert result.level is VerificationLevel.BLOCKED
    assert result.pieces[1].status is PieceStatus.UNAVAILABLE


def test_piece_mismatch_never_becomes_full_verified(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abXd")
    meta = _meta((_file("media.bin", 4, 0),), b"abcd")

    result = verify_v1_pieces(
        meta,
        (V1FileMapping("media.bin", FileMappingState.MAPPED, media),),
    )

    assert result.level is VerificationLevel.CLIENT_CHECK_REQUIRED
    assert result.pieces[0].status is PieceStatus.MISMATCH


def test_symlink_source_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"abcd")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    meta = _meta((_file("link.bin", 4, 0),), b"abcd")

    with pytest.raises(DomainViolation) as caught:
        verify_v1_pieces(
            meta,
            (V1FileMapping("link.bin", FileMappingState.MAPPED, link),),
        )
    assert caught.value.code is ErrorCode.PATH_MAPPING_INVALID


def test_duplicate_mapping_is_ambiguous(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    meta = _meta((_file("media.bin", 4, 0),), b"abcd")

    with pytest.raises(DomainViolation) as caught:
        verify_v1_pieces(
            meta,
            (
                V1FileMapping("media.bin", FileMappingState.MAPPED, media),
                V1FileMapping("media.bin", FileMappingState.MAPPED, media),
            ),
        )
    assert caught.value.code is ErrorCode.MAPPING_AMBIGUOUS


def test_source_change_after_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    meta = _meta((_file("media.bin", 4, 0),), b"abcd")
    original_read = os.read
    changed = False

    def read_and_touch(fd: int, size: int) -> bytes:
        nonlocal changed
        value = original_read(fd, size)
        if not changed:
            changed = True
            media.write_bytes(b"abcd")
            os.utime(media, ns=(media.stat().st_atime_ns, media.stat().st_mtime_ns + 1_000_000))
        return value

    monkeypatch.setattr(os, "read", read_and_touch)
    with pytest.raises(DomainViolation) as caught:
        verify_v1_pieces(
            meta,
            (V1FileMapping("media.bin", FileMappingState.MAPPED, media),),
        )
    assert caught.value.code is ErrorCode.SOURCE_CHANGED
