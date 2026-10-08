import hashlib
from collections.abc import Mapping, Sequence

import pytest

from backend.app.domain.unpack_auxiliary import (
    build_auxiliary_fetch_plan,
    is_auxiliary_torrent_path,
    torrent_client_hash,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


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


def _torrent() -> bytes:
    movie = b"abcdefgh"
    nfo = b"info"
    poster = b"jpg"
    stream = movie + nfo + poster
    pieces = b"".join(
        hashlib.sha1(stream[offset : offset + 4]).digest() for offset in range(0, len(stream), 4)
    )
    return _bencode(
        {
            b"info": {
                b"files": [
                    {b"length": len(movie), b"path": [b"Movie.mkv"]},
                    {b"length": len(nfo), b"path": [b"Movie.nfo"]},
                    {b"length": len(poster), b"path": [b"poster.jpg"]},
                ],
                b"name": b"Release",
                b"piece length": 4,
                b"pieces": pieces,
            }
        }
    )


def test_auxiliary_plan_wants_only_explicit_missing_sidecars() -> None:
    meta = parse_torrent(_torrent())

    plan = build_auxiliary_fetch_plan(
        meta,
        ("Release/Movie.nfo", "Release/poster.jpg"),
    )

    assert plan.wanted_indices == (1, 2)
    assert plan.unwanted_indices == (0,)
    assert plan.missing_paths == ("Release/Movie.nfo", "Release/poster.jpg")
    assert torrent_client_hash(meta) == meta.v1_info_hash


def test_auxiliary_plan_rejects_video_file() -> None:
    meta = parse_torrent(_torrent())

    with pytest.raises(ValueError, match="非辅助文件"):
        build_auxiliary_fetch_plan(meta, ("Release/Movie.mkv",))


def test_auxiliary_path_whitelist_is_explicit() -> None:
    assert is_auxiliary_torrent_path("Release/Movie.nfo") is True
    assert is_auxiliary_torrent_path("Release/poster.PNG") is True
    assert is_auxiliary_torrent_path("Release/extra.mkv") is False
