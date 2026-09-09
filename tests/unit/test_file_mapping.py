import os
import unicodedata
from pathlib import Path

from backend.app.domain.file_mapping import MappingMethod, auto_map_files
from backend.app.domain.torrent import TorrentFile, TorrentKind, TorrentMeta
from backend.app.domain.verification import FileMappingState
from backend.app.infrastructure.source_inventory import scan_source_inventory


def _file(path: str, length: int, order: int, *, padding: bool = False) -> TorrentFile:
    return TorrentFile(path, (path.encode().hex(),), length, padding, length == 0, order)


def _meta(files: tuple[TorrentFile, ...], *, name: str = "Pack") -> TorrentMeta:
    return TorrentMeta(
        torrent_kind=TorrentKind.V1,
        v1_info_hash="00" * 20,
        v2_info_hash=None,
        piece_length=16,
        files=files,
        v1_piece_hashes=(),
        v2_piece_layers=(),
        private=False,
        source=None,
        display_name=name,
        metainfo_digest="11" * 32,
        info_span=(0, 0),
    )


def test_exact_relative_path_wins_even_when_basename_is_not_unique(tmp_path: Path) -> None:
    wanted = tmp_path / "Season 01" / "episode.mkv"
    other = tmp_path / "Extras" / "episode.mkv"
    wanted.parent.mkdir()
    other.parent.mkdir()
    wanted.write_bytes(b"abcd")
    other.write_bytes(b"wxyz")
    meta = _meta((_file("Pack/Season 01/episode.mkv", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.MAPPED
    assert result[0].method is MappingMethod.EXACT_PATH
    assert result[0].source_path == str(wanted)


def test_unique_basename_and_length_is_used_as_second_strategy(tmp_path: Path) -> None:
    source = tmp_path / "local-name" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"abcd")
    meta = _meta((_file("Pack/Release/movie.mkv", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.MAPPED
    assert result[0].method is MappingMethod.BASENAME
    assert result[0].source_path == str(source)


def test_equal_basename_candidates_are_ambiguous_not_first_match(tmp_path: Path) -> None:
    for folder in ("A", "B"):
        path = tmp_path / folder / "movie.mkv"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"abcd")
    meta = _meta((_file("Pack/Unknown/movie.mkv", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.AMBIGUOUS
    assert result[0].method is MappingMethod.BASENAME
    assert len(result[0].candidate_paths) == 2


def test_length_mismatch_is_never_auto_mapped(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"abcde")
    meta = _meta((_file("movie.mkv", 4, 0),), name="movie.mkv")

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.MISSING


def test_padding_and_zero_length_need_no_source_candidate() -> None:
    meta = _meta((_file("Pack/.pad/4", 4, 0, padding=True), _file("Pack/empty.txt", 0, 1)))

    result = auto_map_files(meta, ())

    assert result[0].state is FileMappingState.PADDING
    assert result[1].state is FileMappingState.ZERO_LENGTH


def test_inventory_does_not_follow_source_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.mkv"
    real.write_bytes(b"abcd")
    link = tmp_path / "link.mkv"
    link.symlink_to(real)

    inventory = scan_source_inventory(tmp_path)

    assert [item.relative_path for item in inventory] == ["real.mkv"]


def test_unicode_normalization_collision_remains_ambiguous(tmp_path: Path) -> None:
    composed = "é.mkv"
    decomposed = unicodedata.normalize("NFD", composed)
    if composed == decomposed:
        return
    first = tmp_path / "A" / composed
    second = tmp_path / "B" / decomposed
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"abcd")
    second.write_bytes(b"abcd")
    meta = _meta((_file(f"Pack/Unknown/{composed}", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.AMBIGUOUS


def test_inventory_snapshot_tracks_identity_and_mtime(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"abcd")

    item = scan_source_inventory(tmp_path)[0]
    actual = os.stat(source, follow_symlinks=False)

    assert item.snapshot.device == actual.st_dev
    assert item.snapshot.inode == actual.st_ino
    assert item.snapshot.size == 4
    assert item.snapshot.mtime_ns == actual.st_mtime_ns
    assert item.snapshot.file_type == "regular"


def test_media_token_mapping_handles_canonical_release_variants(tmp_path: Path) -> None:
    source = tmp_path / "different" / "Movie.4K.WEBDL.HEVC.mkv"
    source.parent.mkdir()
    source.write_bytes(b"abcd")
    meta = _meta((_file("Pack/Movie.2160p.WEB-DL.H.265.mkv", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.MAPPED
    assert result[0].method is MappingMethod.MEDIA_TOKENS


def test_media_token_mapping_requires_same_extension(tmp_path: Path) -> None:
    source = tmp_path / "Movie.4K.WEBDL.HEVC.mp4"
    source.write_bytes(b"abcd")
    meta = _meta((_file("Pack/Movie.2160p.WEB-DL.H.265.mkv", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.MISSING


def test_media_token_mapping_does_not_break_ties_by_scan_order(tmp_path: Path) -> None:
    first = tmp_path / "a" / "Movie.4K.WEBDL.HEVC.mkv"
    second = tmp_path / "b" / "movie.2160p.web-dl.h265.mkv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"abcd")
    second.write_bytes(b"abcd")
    meta = _meta((_file("Pack/Movie.2160p.WEB-DL.H.265.mkv", 4, 0),))

    result = auto_map_files(meta, scan_source_inventory(tmp_path))

    assert result[0].state is FileMappingState.AMBIGUOUS
    assert result[0].method is MappingMethod.MEDIA_TOKENS
    assert len(result[0].candidate_paths) == 2
