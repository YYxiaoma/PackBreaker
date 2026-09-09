import hashlib
import unicodedata
from collections.abc import Mapping, Sequence

import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.torrent import TorrentKind
from backend.app.infrastructure.torrent_parser import BencodeLimits, parse_torrent


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


def _torrent(
    info: dict[bytes, object], top: dict[bytes, object] | None = None
) -> tuple[bytes, bytes]:
    info_bytes = _bencode(info)
    payload = _bencode({b"announce": b"https://tracker.invalid/a", b"info": info, **(top or {})})
    return payload, info_bytes


def test_v1_single_file_preserves_raw_info_hash_and_span() -> None:
    content = b"synthetic-media"
    piece_length = 8
    hashes = b"".join(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    payload, info_bytes = _torrent(
        {
            b"length": len(content),
            b"name": b"movie.mkv",
            b"piece length": piece_length,
            b"pieces": hashes,
            b"private": 1,
            b"source": b"SYNTHETIC",
        }
    )

    meta = parse_torrent(payload)

    assert meta.torrent_kind is TorrentKind.V1
    assert meta.v1_info_hash == hashlib.sha1(info_bytes).hexdigest()
    assert payload[slice(*meta.info_span)] == info_bytes
    assert meta.v2_info_hash is None
    assert meta.private is True
    assert meta.source == "SYNTHETIC"
    assert meta.files[0].path == "movie.mkv"
    assert meta.files[0].length == len(content)


def test_v1_multifile_normalizes_unicode_and_marks_padding() -> None:
    decomposed = unicodedata.normalize("NFD", "épisode.mkv").encode()
    info = {
        b"files": [
            {b"length": 3, b"path": [b"Season 01", decomposed]},
            {b"attr": b"p", b"length": 1, b"path": [b".pad", b"0"]},
        ],
        b"name": b"Show",
        b"piece length": 4,
        b"pieces": hashlib.sha1(b"abcd").digest(),
    }
    payload, _ = _torrent(info)

    meta = parse_torrent(payload)

    assert [file.path for file in meta.files] == ["Show/Season 01/épisode.mkv", "Show/.pad/0"]
    assert meta.files[1].padding is True
    assert meta.files[0].raw_path_hex[-1] == decomposed.hex()


def test_v2_file_tree_and_piece_layers_are_parsed() -> None:
    piece_length = 16 * 1024
    first = hashlib.sha256(b"piece-a").digest()
    second = hashlib.sha256(b"piece-b").digest()
    root = hashlib.sha256(first + second).digest()
    layer = first + second
    info = {
        b"file tree": {
            b"movie.mkv": {b"": {b"length": piece_length + 1, b"pieces root": root}},
            b"zero.txt": {b"": {b"length": 0}},
        },
        b"meta version": 2,
        b"name": b"Pack",
        b"piece length": piece_length,
    }
    payload, info_bytes = _torrent(info, {b"piece layers": {root: layer}})

    meta = parse_torrent(payload)

    assert meta.torrent_kind is TorrentKind.V2
    assert meta.v1_info_hash is None
    assert meta.v2_info_hash == hashlib.sha256(info_bytes).hexdigest()
    assert [file.path for file in meta.files] == ["Pack/movie.mkv", "Pack/zero.txt"]
    assert meta.files[1].zero_length is True
    assert meta.v2_piece_layers[0].pieces_root == root
    assert len(meta.v2_piece_layers[0].hashes) == 2


def test_v2_piece_layer_must_reconstruct_pieces_root() -> None:
    piece_length = 16 * 1024
    root = hashlib.sha256(b"wrong-root").digest()
    layer = hashlib.sha256(b"piece-a").digest() + hashlib.sha256(b"piece-b").digest()
    info = {
        b"file tree": {b"movie.mkv": {b"": {b"length": piece_length + 1, b"pieces root": root}}},
        b"meta version": 2,
        b"name": b"Pack",
        b"piece length": piece_length,
    }
    payload, _ = _torrent(info, {b"piece layers": {root: layer}})

    with pytest.raises(DomainViolation) as failure:
        parse_torrent(payload)

    assert failure.value.code is ErrorCode.TORRENT_META_INVALID


def test_consistent_hybrid_uses_v2_files_and_both_hashes() -> None:
    piece_length = 16 * 1024
    content = b"abc"
    root = hashlib.sha256(content).digest()
    info = {
        b"file tree": {b"movie.mkv": {b"": {b"length": 3, b"pieces root": root}}},
        b"files": [{b"length": 3, b"path": [b"movie.mkv"]}],
        b"meta version": 2,
        b"name": b"Pack",
        b"piece length": piece_length,
        b"pieces": hashlib.sha1(content).digest(),
    }
    payload, _ = _torrent(info)

    meta = parse_torrent(payload)

    assert meta.torrent_kind is TorrentKind.HYBRID
    assert meta.v1_info_hash is not None and meta.v2_info_hash is not None
    assert meta.files[0].path == "Pack/movie.mkv"


def test_inconsistent_hybrid_is_blocked() -> None:
    piece_length = 16 * 1024
    root = hashlib.sha256(b"abc").digest()
    info = {
        b"file tree": {b"movie.mkv": {b"": {b"length": 4, b"pieces root": root}}},
        b"files": [{b"length": 3, b"path": [b"movie.mkv"]}],
        b"meta version": 2,
        b"name": b"Pack",
        b"piece length": piece_length,
        b"pieces": hashlib.sha1(b"abc").digest(),
    }
    payload, _ = _torrent(info)

    with pytest.raises(DomainViolation) as failure:
        parse_torrent(payload)

    assert failure.value.code is ErrorCode.TORRENT_HYBRID_INCONSISTENT


@pytest.mark.parametrize(
    "path",
    [
        [b"..", b"escape.mkv"],
        [b"folder/escape.mkv"],
        [b"folder\\escape.mkv"],
        [b"\xff.mkv"],
    ],
)
def test_unsafe_v1_paths_are_blocked(path: list[bytes]) -> None:
    info = {
        b"files": [{b"length": 1, b"path": path}],
        b"name": b"Pack",
        b"piece length": 1,
        b"pieces": hashlib.sha1(b"x").digest(),
    }
    payload, _ = _torrent(info)

    with pytest.raises(DomainViolation) as failure:
        parse_torrent(payload)

    assert failure.value.code is ErrorCode.UNSAFE_TORRENT_PATH


def test_duplicate_dictionary_keys_and_noncanonical_integer_are_rejected() -> None:
    duplicate = b"d4:infod4:name1:a4:name1:bee"
    with pytest.raises(DomainViolation) as duplicate_failure:
        parse_torrent(duplicate)
    assert duplicate_failure.value.code is ErrorCode.TORRENT_META_INVALID

    noncanonical = (
        b"d4:infod6:lengthi01e4:name1:a12:piece lengthi1e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"
    )
    with pytest.raises(DomainViolation) as integer_failure:
        parse_torrent(noncanonical)
    assert integer_failure.value.code is ErrorCode.TORRENT_META_INVALID


def test_parser_enforces_payload_limit_before_decoding() -> None:
    payload = b"d4:infode"

    with pytest.raises(DomainViolation) as failure:
        parse_torrent(payload, limits=BencodeLimits(max_payload_bytes=5))

    assert failure.value.code is ErrorCode.TORRENT_LIMIT_EXCEEDED
