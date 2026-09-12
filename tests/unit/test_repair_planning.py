from __future__ import annotations

import hashlib
from pathlib import Path

from backend.app.application.analysis import verify_torrent_evidence
from backend.app.domain.file_mapping import AutoMappingDecision, MappingMethod
from backend.app.domain.repair import (
    RepairActionKind,
    RepairBlockReason,
    RepairMode,
    RepairPieceScope,
    RepairTargetEvidence,
    build_repair_plan,
)
from backend.app.domain.torrent import TorrentFile, TorrentKind, TorrentMeta
from backend.app.domain.verification import (
    FileMappingEvidence,
    FileMappingState,
    FileSnapshot,
    PieceEvidence,
    PieceStatus,
    V1VerificationResult,
    V2FileEvidence,
    V2VerificationResult,
    VerificationLevel,
)
from backend.app.infrastructure.piece_verifier import V1FileMapping, verify_v1_pieces


def _file(path: str, length: int, order: int) -> TorrentFile:
    return TorrentFile(path, (path.encode().hex(),), length, False, length == 0, order)


def _v1_meta(
    files: tuple[TorrentFile, ...], content: bytes, *, piece_length: int = 4
) -> TorrentMeta:
    return TorrentMeta(
        torrent_kind=TorrentKind.V1,
        v1_info_hash="11" * 20,
        v2_info_hash=None,
        piece_length=piece_length,
        files=files,
        v1_piece_hashes=tuple(
            hashlib.sha1(content[offset : offset + piece_length]).digest()
            for offset in range(0, len(content), piece_length)
        ),
        v2_piece_layers=(),
        private=False,
        source=None,
        display_name="repair-fixture",
        metainfo_digest="22" * 32,
        info_span=(0, 0),
    )


def _target(
    mapping: FileMappingEvidence,
    *,
    shared: bool,
    available_bytes: int = 1024,
) -> RepairTargetEvidence:
    assert mapping.snapshot is not None
    snapshot = mapping.snapshot
    return RepairTargetEvidence(
        torrent_path=mapping.torrent_path,
        expected_length=snapshot.size,
        target_exists=True,
        target_device=snapshot.device,
        target_inode=snapshot.inode if shared else snapshot.inode + 1000,
        target_size=snapshot.size,
        target_link_count=2 if shared else 1,
        source_device=snapshot.device,
        source_inode=snapshot.inode,
        available_bytes=available_bytes,
    )


def _cross_file_fixture(tmp_path: Path) -> tuple[TorrentMeta, V1VerificationResult]:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"abc")
    second.write_bytes(b"Xefgh")
    meta = _v1_meta((_file("a.bin", 3, 0), _file("b.bin", 5, 1)), b"abcdefgh")
    verification = verify_v1_pieces(
        meta,
        (
            V1FileMapping("a.bin", FileMappingState.MAPPED, first),
            V1FileMapping("b.bin", FileMappingState.MAPPED, second),
        ),
    )
    assert verification.pieces[0].status is PieceStatus.MISMATCH
    assert verification.pieces[0].covered_files == ("a.bin", "b.bin")
    return meta, verification


def test_auto_piece_plan_detects_cross_file_piece_and_requires_inode_isolation(
    tmp_path: Path,
) -> None:
    meta, verification = _cross_file_fixture(tmp_path)
    targets = tuple(_target(mapping, shared=True) for mapping in verification.mappings)

    plan = build_repair_plan(
        meta,
        verification,
        targets,
        mode=RepairMode.AUTO_PIECE,
        downloader_paused=True,
    )

    assert plan.ready is True
    assert plan.execution_allowed is False
    assert plan.isolation_bytes_required == 8
    assert plan.required_free_bytes == 8
    assert plan.estimated_download_bytes_upper_bound == 8
    assert len(plan.cross_file_pieces) == 1
    assert plan.cross_file_pieces[0].scope is RepairPieceScope.V1_STREAM
    assert plan.cross_file_pieces[0].covered_files == ("a.bin", "b.bin")
    assert {item.torrent_path for item in plan.affected_files} == {"a.bin", "b.bin"}
    assert all(item.isolation_required for item in plan.affected_files)
    assert [item.kind for item in plan.actions].count(RepairActionKind.ISOLATE_TARGET) == 2
    assert plan.actions[-1].kind is RepairActionKind.REPAIR_PIECES


def test_file_only_plan_blocks_cross_file_piece_and_shared_inode(tmp_path: Path) -> None:
    meta, verification = _cross_file_fixture(tmp_path)
    targets = tuple(_target(mapping, shared=True) for mapping in verification.mappings)

    plan = build_repair_plan(
        meta,
        verification,
        targets,
        mode=RepairMode.FILE_ONLY,
        downloader_paused=True,
    )

    assert plan.ready is False
    assert set(plan.blocked_reasons) == {
        RepairBlockReason.FILE_ONLY_CROSS_FILE_PIECE,
        RepairBlockReason.FILE_ONLY_REQUIRES_ISOLATION,
    }
    assert plan.execution_allowed is False


def test_auto_piece_plan_requires_paused_downloader_and_space(tmp_path: Path) -> None:
    meta, verification = _cross_file_fixture(tmp_path)
    targets = tuple(
        _target(mapping, shared=True, available_bytes=7) for mapping in verification.mappings
    )

    plan = build_repair_plan(
        meta,
        verification,
        targets,
        mode=RepairMode.AUTO_PIECE,
        downloader_paused=False,
    )

    assert plan.ready is False
    assert RepairBlockReason.DOWNLOADER_NOT_PAUSED in plan.blocked_reasons
    assert RepairBlockReason.INSUFFICIENT_SPACE in plan.blocked_reasons
    assert plan.required_free_bytes == 8
    assert plan.available_bytes == 7


def test_missing_sidecar_becomes_target_only_fetch_without_source_write(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    meta = _v1_meta((_file("media.bin", 4, 0), _file("extra.nfo", 4, 1)), b"abcdefgh")
    verification = verify_v1_pieces(
        meta,
        (
            V1FileMapping("media.bin", FileMappingState.MAPPED, media),
            V1FileMapping("extra.nfo", FileMappingState.MISSING),
        ),
    )
    target = RepairTargetEvidence(
        torrent_path="extra.nfo",
        expected_length=4,
        target_exists=False,
        target_device=None,
        target_inode=None,
        target_size=None,
        target_link_count=None,
        source_device=None,
        source_inode=None,
        available_bytes=100,
    )

    plan = build_repair_plan(
        meta,
        verification,
        (target,),
        mode=RepairMode.AUTO_PIECE,
        downloader_paused=True,
    )

    assert plan.ready is True
    assert plan.required_free_bytes == 4
    assert plan.isolation_bytes_required == 0
    assert plan.estimated_download_bytes_upper_bound == 4
    assert len(plan.affected_files) == 1
    assert plan.affected_files[0].mapping_state is FileMappingState.MISSING
    assert plan.affected_files[0].whole_file_fetch is True
    assert RepairActionKind.FETCH_FILE in {item.kind for item in plan.actions}
    assert plan.execution_allowed is False


def test_v2_piece_mismatch_is_file_local_and_does_not_create_cross_file_risk() -> None:
    first = _file("a.bin", 4, 0)
    second = _file("b.bin", 4, 1)
    meta = TorrentMeta(
        torrent_kind=TorrentKind.V2,
        v1_info_hash=None,
        v2_info_hash="33" * 32,
        piece_length=16 * 1024,
        files=(first, second),
        v1_piece_hashes=(),
        v2_piece_layers=(),
        private=False,
        source=None,
        display_name="v2-repair",
        metainfo_digest="44" * 32,
        info_span=(0, 0),
    )
    first_snapshot = FileSnapshot(1, 10, 4, 100)
    second_snapshot = FileSnapshot(1, 11, 4, 100)
    verification = V2VerificationResult(
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        (
            FileMappingEvidence("a.bin", FileMappingState.MAPPED, "/src/a.bin", first_snapshot),
            FileMappingEvidence("b.bin", FileMappingState.MAPPED, "/src/b.bin", second_snapshot),
        ),
        (
            V2FileEvidence(
                "a.bin",
                PieceStatus.MISMATCH,
                (PieceEvidence(0, PieceStatus.MISMATCH, ("a.bin",)),),
            ),
            V2FileEvidence(
                "b.bin",
                PieceStatus.VERIFIED,
                (PieceEvidence(0, PieceStatus.VERIFIED, ("b.bin",)),),
            ),
        ),
    )
    target = RepairTargetEvidence("a.bin", 4, True, 1, 99, 4, 1, 1, 10, 100)

    plan = build_repair_plan(
        meta,
        verification,
        (target,),
        mode=RepairMode.FILE_ONLY,
        downloader_paused=True,
    )

    assert plan.ready is True
    assert plan.cross_file_pieces == ()
    assert plan.affected_pieces[0].scope is RepairPieceScope.V2_FILE
    assert plan.affected_files[0].shares_source_inode is False
    assert plan.actions == (plan.actions[0],)
    assert plan.actions[0].kind is RepairActionKind.REPAIR_FILE
    assert plan.execution_allowed is False


def test_file_only_plan_requires_isolation_for_any_multi_link_target() -> None:
    meta = TorrentMeta(
        torrent_kind=TorrentKind.V2,
        v1_info_hash=None,
        v2_info_hash="55" * 32,
        piece_length=16 * 1024,
        files=(_file("movie.bin", 4, 0),),
        v1_piece_hashes=(),
        v2_piece_layers=(),
        private=False,
        source=None,
        display_name="multi-link",
        metainfo_digest="66" * 32,
        info_span=(0, 0),
    )
    snapshot = FileSnapshot(1, 10, 4, 100)
    verification = V2VerificationResult(
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        (FileMappingEvidence("movie.bin", FileMappingState.MAPPED, "/src/movie.bin", snapshot),),
        (
            V2FileEvidence(
                "movie.bin",
                PieceStatus.MISMATCH,
                (PieceEvidence(0, PieceStatus.MISMATCH, ("movie.bin",)),),
            ),
        ),
    )
    target = RepairTargetEvidence(
        "movie.bin",
        4,
        True,
        1,
        99,
        4,
        2,
        1,
        10,
        100,
    )

    plan = build_repair_plan(
        meta,
        verification,
        (target,),
        mode=RepairMode.FILE_ONLY,
        downloader_paused=True,
    )

    assert plan.affected_files[0].shares_source_inode is False
    assert plan.affected_files[0].isolation_required is True
    assert plan.blocked_reasons == (RepairBlockReason.FILE_ONLY_REQUIRES_ISOLATION,)
    assert plan.execution_allowed is False


def test_verify_torrent_evidence_exposes_piece_level_result_for_repair(tmp_path: Path) -> None:
    media = tmp_path / "movie.bin"
    media.write_bytes(b"abXd")
    meta = _v1_meta((_file("movie.bin", 4, 0),), b"abcd")
    stat_result = media.stat(follow_symlinks=False)
    mapping = AutoMappingDecision(
        torrent_path="movie.bin",
        state=FileMappingState.MAPPED,
        method=MappingMethod.EXACT_PATH,
        source_path=str(media),
        snapshot=FileSnapshot(
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        ),
        candidate_paths=(str(media),),
    )

    result = verify_torrent_evidence(meta, (mapping,))

    assert isinstance(result, V1VerificationResult)
    assert result.level is VerificationLevel.CLIENT_CHECK_REQUIRED
    assert result.pieces[0].status is PieceStatus.MISMATCH
    assert result.pieces[0].covered_files == ("movie.bin",)


def test_guided_plan_does_not_require_pause_or_automatic_space_gate(tmp_path: Path) -> None:
    meta, verification = _cross_file_fixture(tmp_path)
    targets = tuple(
        _target(mapping, shared=True, available_bytes=0) for mapping in verification.mappings
    )

    plan = build_repair_plan(
        meta,
        verification,
        targets,
        mode=RepairMode.GUIDED,
        downloader_paused=False,
    )

    assert plan.ready is True
    assert plan.blocked_reasons == ()
    assert plan.actions == (plan.actions[0],)
    assert plan.actions[0].kind is RepairActionKind.MANUAL_GUIDANCE
    assert plan.execution_allowed is False


def test_fully_verified_candidate_reports_no_repair_needed(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"abcd")
    meta = _v1_meta((_file("media.bin", 4, 0),), b"abcd")
    verification = verify_v1_pieces(
        meta,
        (V1FileMapping("media.bin", FileMappingState.MAPPED, media),),
    )
    target = _target(verification.mappings[0], shared=True)

    plan = build_repair_plan(
        meta,
        verification,
        (target,),
        mode=RepairMode.AUTO_PIECE,
        downloader_paused=True,
    )

    assert plan.ready is False
    assert plan.blocked_reasons == (RepairBlockReason.NO_REPAIR_NEEDED,)
    assert plan.affected_files == ()
    assert plan.actions == ()
    assert plan.execution_allowed is False
