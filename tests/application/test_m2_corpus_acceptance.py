import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from backend.app.application.m2_corpus_acceptance import run_m2_corpus_acceptance
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.verification import VerificationLevel


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: item[0])
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in items) + b"e"
    raise TypeError(type(value).__name__)


def _write_v1_torrent(path: Path, content: bytes) -> None:
    piece_length = 4
    pieces = b"".join(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    path.write_bytes(
        _bencode(
            {
                b"announce": b"https://private-tracker.invalid/secret-passkey",
                b"info": {
                    b"length": len(content),
                    b"name": b"movie.mkv",
                    b"piece length": piece_length,
                    b"pieces": pieces,
                    b"private": 1,
                    b"source": b"PRIVATE-SOURCE",
                },
            }
        )
    )


def test_acceptance_is_stable_and_full_verified_without_source_changes(tmp_path: Path) -> None:
    content = b"abcdefgh"
    torrent = tmp_path / "input.torrent"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "movie.mkv"
    source.write_bytes(content)
    _write_v1_torrent(torrent, content)
    before = source.stat(follow_symlinks=False)

    report = run_m2_corpus_acceptance(
        torrent_path=torrent,
        source_root=source_root,
        repeated_runs=3,
        verify_content=True,
    )

    after = source.stat(follow_symlinks=False)
    assert report.status == "PASS"
    assert report.inventory_stable is True
    assert report.mapping_stable is True
    assert report.verification_level is VerificationLevel.FULL_VERIFIED
    assert report.source_unchanged_after_verification is True
    assert report.execution_allowed is False
    assert report.side_effects_started is False
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def test_acceptance_report_does_not_echo_tracker_source_or_info_hash(tmp_path: Path) -> None:
    content = b"abcd"
    torrent = tmp_path / "input.torrent"
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "movie.mkv").write_bytes(content)
    _write_v1_torrent(torrent, content)

    report = run_m2_corpus_acceptance(torrent_path=torrent, source_root=source_root)
    serialized = json.dumps(report.to_payload(), ensure_ascii=False)

    assert "secret-passkey" not in serialized
    assert "PRIVATE-SOURCE" not in serialized
    assert "info_hash" not in serialized
    assert report.verification_performed is False


def test_acceptance_reports_missing_mapping_without_hashing_content(tmp_path: Path) -> None:
    torrent = tmp_path / "input.torrent"
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_v1_torrent(torrent, b"abcd")

    report = run_m2_corpus_acceptance(torrent_path=torrent, source_root=source_root)

    assert dict(report.mapping_state_counts)["MISSING"] == 1
    assert report.verification_performed is False
    assert report.verification_level is None
    assert report.status == "PASS"


def test_acceptance_rejects_torrent_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.torrent"
    _write_v1_torrent(real, b"abcd")
    link = tmp_path / "input.torrent"
    link.symlink_to(real)
    source_root = tmp_path / "source"
    source_root.mkdir()

    with pytest.raises(DomainViolation) as failure:
        run_m2_corpus_acceptance(torrent_path=link, source_root=source_root)

    assert failure.value.code is ErrorCode.PATH_MAPPING_INVALID


def test_acceptance_requires_multiple_stability_runs(tmp_path: Path) -> None:
    torrent = tmp_path / "input.torrent"
    _write_v1_torrent(torrent, b"abcd")
    source_root = tmp_path / "source"
    source_root.mkdir()

    with pytest.raises(ValueError, match="至少为 2"):
        run_m2_corpus_acceptance(
            torrent_path=torrent,
            source_root=source_root,
            repeated_runs=1,
        )
