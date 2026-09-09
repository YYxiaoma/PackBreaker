import os
from pathlib import Path

import pytest

from backend.app.domain.file_mapping import AutoMappingDecision, MappingMethod, auto_map_files
from backend.app.domain.torrent import TorrentFile, TorrentKind, TorrentMeta
from backend.app.domain.verification import FileMappingState
from backend.app.infrastructure.source_inventory import scan_source_inventory
from backend.app.infrastructure.verification_cache import (
    VerificationResultCache,
    build_verification_cache_key,
    mapping_snapshots_current,
)


def _meta(digest: str = "11" * 32) -> TorrentMeta:
    file = TorrentFile("movie.mkv", ("00",), 4, False, False, 0)
    return TorrentMeta(
        torrent_kind=TorrentKind.V1,
        v1_info_hash="00" * 20,
        v2_info_hash=None,
        piece_length=4,
        files=(file,),
        v1_piece_hashes=(),
        v2_piece_layers=(),
        private=False,
        source=None,
        display_name="movie.mkv",
        metainfo_digest=digest,
        info_span=(0, 0),
    )


def _mapped(tmp_path: Path) -> tuple[Path, tuple[AutoMappingDecision, ...]]:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"abcd")
    mappings = auto_map_files(_meta(), scan_source_inventory(tmp_path))
    return source, mappings


def test_cache_key_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    _, mappings = _mapped(tmp_path)

    first = build_verification_cache_key(_meta(), mappings)
    second = build_verification_cache_key(_meta(), mappings)

    assert first == second


def test_metainfo_algorithm_and_read_policy_change_cache_key(tmp_path: Path) -> None:
    _, mappings = _mapped(tmp_path)
    baseline = build_verification_cache_key(_meta(), mappings)

    assert build_verification_cache_key(_meta("22" * 32), mappings).digest != baseline.digest
    assert (
        build_verification_cache_key(_meta(), mappings, algorithm_version="next").digest
        != baseline.digest
    )
    assert (
        build_verification_cache_key(_meta(), mappings, read_policy="next").digest
        != baseline.digest
    )


def test_cache_hit_requires_current_source_snapshot(tmp_path: Path) -> None:
    source, mappings = _mapped(tmp_path)
    key = build_verification_cache_key(_meta(), mappings)
    cache: VerificationResultCache[str] = VerificationResultCache()
    cache.put(key, "FULL_VERIFIED")

    assert mapping_snapshots_current(mappings) is True
    assert cache.get_if_current(_meta(), mappings) == "FULL_VERIFIED"

    before = source.stat()
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    assert mapping_snapshots_current(mappings) is False
    assert cache.get_if_current(_meta(), mappings) is None


def test_replaced_inode_invalidates_cached_result_even_with_same_bytes(tmp_path: Path) -> None:
    source, mappings = _mapped(tmp_path)
    cache: VerificationResultCache[str] = VerificationResultCache()
    key = build_verification_cache_key(_meta(), mappings)
    cache.put(key, "FULL_VERIFIED")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"abcd")
    replacement.replace(source)

    assert cache.get_if_current(_meta(), mappings) is None


def test_cache_key_requires_complete_torrent_order(tmp_path: Path) -> None:
    _, mappings = _mapped(tmp_path)
    wrong_path = AutoMappingDecision(
        "other.mkv",
        mappings[0].state,
        mappings[0].method,
        mappings[0].source_path,
        mappings[0].snapshot,
        mappings[0].candidate_paths,
    )

    with pytest.raises(ValueError, match="完整覆盖"):
        build_verification_cache_key(_meta(), ())
    with pytest.raises(ValueError, match="完整覆盖"):
        build_verification_cache_key(_meta(), (wrong_path,))


def test_ambiguous_candidate_evidence_changes_cache_key() -> None:
    first = AutoMappingDecision(
        "movie.mkv",
        FileMappingState.AMBIGUOUS,
        MappingMethod.BASENAME,
        None,
        None,
        ("/source/A/movie.mkv", "/source/B/movie.mkv"),
    )
    second = AutoMappingDecision(
        "movie.mkv",
        FileMappingState.AMBIGUOUS,
        MappingMethod.BASENAME,
        None,
        None,
        ("/source/A/movie.mkv", "/source/C/movie.mkv"),
    )

    assert (
        build_verification_cache_key(_meta(), (first,)).digest
        != build_verification_cache_key(_meta(), (second,)).digest
    )
