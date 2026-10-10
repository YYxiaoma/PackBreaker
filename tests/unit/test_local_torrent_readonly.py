from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.app.domain.torrent import TorrentMeta
from backend.app.domain.verification import V1VerificationResult
from backend.app.infrastructure.piece_verifier import V1FileMapping, verify_v1_pieces
from scripts import check_local_torrent_readonly as probe


def _bencode(val: object) -> bytes:
    if isinstance(val, int):
        return f"i{val}e".encode()
    if isinstance(val, bytes):
        return str(len(val)).encode() + b":" + val
    if isinstance(val, dict):
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in sorted(val.items())) + b"e"
    raise TypeError(type(val).__name__)


def _fixture(tmp_path: Path, *, mismatch: bool = False) -> tuple[Path, Path, Path]:
    root = tmp_path / "data"
    folder = root / "Movie.2024"
    folder.mkdir(parents=True)
    source = folder / "Movie.2024.mkv"
    source.write_bytes(b"abcdefgh")
    pieces = b"".join(hashlib.sha1(x).digest() for x in (b"abcd", b"efgh"))
    if mismatch:
        pieces = b"0" * len(pieces)
    payload = _bencode(
        {b"info": {b"length": 8, b"name": b"Movie.2024.mkv", b"piece length": 4, b"pieces": pieces}}
    )
    torrent = tmp_path / "upload.torrent"
    torrent.write_bytes(payload)
    torrent.chmod(0o600)
    return torrent, source, root


@pytest.mark.parametrize(
    ("mismatch", "expected"), [(False, "FULL_VERIFIED"), (True, "CLIENT_CHECK_REQUIRED")]
)
def test_all_pieces_without_network(tmp_path: Path, mismatch: bool, expected: str) -> None:
    torrent, source, root = _fixture(tmp_path, mismatch=mismatch)
    before = source.stat()
    report = probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)
    assert report["status"] == expected
    assert report["pieces_total"] == 2
    assert report["pieces_verified"] == (0 if mismatch else 2)
    assert report["approved_for_client_add"] is False
    assert report["downloader_contacted"] is False
    assert report["tracker_contacted"] is False
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert source.name not in json.dumps(report)
    assert torrent.name not in json.dumps(report)


def test_rejects_symlink_and_insecure_torrent(tmp_path: Path) -> None:
    torrent, source, root = _fixture(tmp_path)
    link = tmp_path / "link.torrent"
    link.symlink_to(torrent)
    assert (
        probe.check_local_torrent(torrent_file=link, source=source, data_root=root)["status"]
        == "TORRENT_FILE_BLOCKED"
    )
    torrent.chmod(0o644)
    assert (
        probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)["status"]
        == "TORRENT_FILE_BLOCKED"
    )


def test_corrupt_and_oversized_upload(tmp_path: Path) -> None:
    torrent, source, root = _fixture(tmp_path)
    torrent.write_bytes(b"invalid")
    assert (
        probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)["status"]
        == "VERIFICATION_FAILED"
    )
    with torrent.open("wb") as stream:
        stream.truncate(probe._MAX_BYTES + 1)
    assert (
        probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)["status"]
        == "TORRENT_FILE_BLOCKED"
    )


def test_symlink_source_and_ambiguous_mapping(tmp_path: Path) -> None:
    torrent, source, root = _fixture(tmp_path)
    link = root / "linked.mkv"
    link.symlink_to(source)
    assert (
        probe.check_local_torrent(torrent_file=torrent, source=link, data_root=root)["status"]
        == "SOURCE_BLOCKED"
    )
    (root / source.name).write_bytes(b"abcdefgh")
    assert (
        probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)["status"]
        == "FILE_MAPPING_INCOMPLETE"
    )


def test_source_mutation_blocks_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    torrent, source, root = _fixture(tmp_path)

    def mutated(meta: TorrentMeta, mappings: tuple[V1FileMapping, ...]) -> V1VerificationResult:
        result = verify_v1_pieces(meta, mappings)
        before = source.stat()
        source.write_bytes(b"ijklmnop")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        return result

    monkeypatch.setattr(probe, "verify_v1_pieces", mutated)
    assert (
        probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)["status"]
        == "SOURCE_SNAPSHOT_CHANGED"
    )


def test_unrelated_source_cannot_claim_verified_torrent(tmp_path: Path) -> None:
    torrent, source, root = _fixture(tmp_path)
    other = source.parent / "Other.2024.mkv"
    other.write_bytes(b"abcdefgh")
    pieces = b"".join(hashlib.sha1(x).digest() for x in (b"abcd", b"efgh"))
    torrent.write_bytes(
        _bencode(
            {
                b"info": {
                    b"length": 8,
                    b"name": b"Other.2024.mkv",
                    b"piece length": 4,
                    b"pieces": pieces,
                }
            }
        )
    )
    report = probe.check_local_torrent(torrent_file=torrent, source=source, data_root=root)
    assert report["status"] == "SOURCE_NOT_REFERENCED"
    assert report["approved_for_client_add"] is False


def test_fifo_torrent_is_rejected_without_blocking(tmp_path: Path) -> None:
    _, source, root = _fixture(tmp_path)
    pipe = tmp_path / "fake.torrent"
    os.mkfifo(pipe, 0o600)
    assert (
        probe.check_local_torrent(torrent_file=pipe, source=source, data_root=root)["status"]
        == "TORRENT_FILE_BLOCKED"
    )
