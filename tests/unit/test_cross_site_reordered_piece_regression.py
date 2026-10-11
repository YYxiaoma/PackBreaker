"""Synthetic regression for differently ordered, differently chunked private torrents.

This deliberately uses generated fixture bytes and a nonexistent tracker origin.
No user torrent, media path, credentials, downloader or site is involved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from backend.app.application.analysis import verify_torrent_mappings
from backend.app.domain.file_mapping import auto_map_files
from backend.app.domain.verification import FileMappingState, VerificationLevel
from backend.app.infrastructure.source_inventory import scan_source_inventory
from backend.app.infrastructure.torrent_parser import parse_torrent


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Mapping):
        return (
            b"d"
            + b"".join(_bencode(key) + _bencode(child) for key, child in sorted(value.items()))
            + b"e"
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    raise TypeError(type(value).__name__)


def _torrent(
    files: Mapping[str, bytes],
    *,
    order: tuple[str, ...],
    piece_length: int,
    source: bytes,
    tracker: bytes,
) -> bytes:
    content = b"".join(files[path] for path in order)
    pieces = b"".join(
        hashlib.sha1(content[i : i + piece_length]).digest()
        for i in range(0, len(content), piece_length)
    )
    info = {
        b"name": b"Synthetic.Release",
        b"files": [{b"length": len(files[name]), b"path": [name.encode()]} for name in order],
        b"piece length": piece_length,
        b"pieces": pieces,
        b"private": 1,
        b"source": source,
    }
    return _bencode({b"announce": tracker, b"info": info})


@pytest.mark.parametrize("mutate_one_source", [False, True])
def test_three_cross_site_torrents_map_by_path_and_verify_in_own_piece_order(
    tmp_path: Path, mutate_one_source: bool
) -> None:
    """Identical file sets do not imply identical Piece streams or InfoHash."""
    source = tmp_path / "source"
    source.mkdir()
    files = {
        "feature.mkv": b"video-data-0123456789",
        "subtitle.srt": b"subtitles",
        "notes.nfo": b"notes!",
    }
    for name, content in files.items():
        (source / name).write_bytes(content)

    normal = ("feature.mkv", "subtitle.srt", "notes.nfo")
    reordered = ("notes.nfo", "feature.mkv", "subtitle.srt")
    metas = {
        "a": parse_torrent(
            _torrent(
                files,
                order=reordered,
                piece_length=16,
                source=b"site-a",
                tracker=b"https://tracker.invalid/a",
            )
        ),
        "b": parse_torrent(
            _torrent(
                files,
                order=normal,
                piece_length=8,
                source=b"site-b",
                tracker=b"https://tracker.invalid/b",
            )
        ),
        "c": parse_torrent(
            _torrent(
                files,
                order=normal,
                piece_length=8,
                source=b"site-c",
                tracker=b"https://tracker.invalid/c",
            )
        ),
    }
    assert len({meta.v1_info_hash for meta in metas.values()}) == 3
    assert metas["b"].v1_piece_hashes == metas["c"].v1_piece_hashes
    assert metas["b"].piece_length == metas["c"].piece_length
    assert metas["a"].piece_length != metas["b"].piece_length
    assert [f.path for f in metas["a"].files] != [f.path for f in metas["b"].files]
    assert {(f.path, f.length) for f in metas["a"].files} == {
        (f.path, f.length) for f in metas["b"].files
    }

    if mutate_one_source:
        # Same file size, different bytes. A plausible mapping must NOT cause
        # FULL_VERIFIED, including when torrent file order is changed.
        (source / "feature.mkv").write_bytes(b"X" + files["feature.mkv"][1:])

    inventory = scan_source_inventory(source)
    for meta in metas.values():
        mapped = auto_map_files(meta, inventory)
        assert len(mapped) == 3
        assert all(item.state is FileMappingState.MAPPED for item in mapped)
        for torrent_file, item in zip(meta.files, mapped, strict=True):
            assert item.torrent_path == torrent_file.path
            assert item.source_path is not None
            assert Path(item.source_path).name == Path(torrent_file.path).name
        assert verify_torrent_mappings(meta, mapped) is (
            VerificationLevel.CLIENT_CHECK_REQUIRED
            if mutate_one_source
            else VerificationLevel.FULL_VERIFIED
        )
