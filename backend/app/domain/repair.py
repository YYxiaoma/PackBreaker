from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.app.domain.torrent import TorrentKind, TorrentMeta
from backend.app.domain.verification import (
    FileMappingEvidence,
    FileMappingState,
    HybridVerificationResult,
    PieceEvidence,
    PieceStatus,
    TorrentVerificationResult,
    V1VerificationResult,
    V2VerificationResult,
    VerificationLevel,
)


class RepairMode(StrEnum):
    AUTO_PIECE = "AUTO_PIECE"
    FILE_ONLY = "FILE_ONLY"
    GUIDED = "GUIDED"


class RepairPieceScope(StrEnum):
    V1_STREAM = "V1_STREAM"
    V2_FILE = "V2_FILE"


class RepairActionKind(StrEnum):
    ISOLATE_TARGET = "ISOLATE_TARGET"
    REPAIR_PIECES = "REPAIR_PIECES"
    FETCH_FILE = "FETCH_FILE"
    REPAIR_FILE = "REPAIR_FILE"
    MANUAL_GUIDANCE = "MANUAL_GUIDANCE"


class RepairBlockReason(StrEnum):
    NO_REPAIR_NEEDED = "NO_REPAIR_NEEDED"
    VERIFICATION_BLOCKED = "VERIFICATION_BLOCKED"
    TARGET_EVIDENCE_MISSING = "TARGET_EVIDENCE_MISSING"
    TARGET_MISSING = "TARGET_MISSING"
    TARGET_LENGTH_MISMATCH = "TARGET_LENGTH_MISMATCH"
    TARGET_SOURCE_IDENTITY_UNKNOWN = "TARGET_SOURCE_IDENTITY_UNKNOWN"
    DOWNLOADER_NOT_PAUSED = "DOWNLOADER_NOT_PAUSED"
    INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
    FILE_ONLY_CROSS_FILE_PIECE = "FILE_ONLY_CROSS_FILE_PIECE"
    FILE_ONLY_REQUIRES_ISOLATION = "FILE_ONLY_REQUIRES_ISOLATION"


@dataclass(frozen=True, slots=True)
class RepairTargetEvidence:
    torrent_path: str
    expected_length: int
    target_exists: bool
    target_device: int | None
    target_inode: int | None
    target_size: int | None
    target_link_count: int | None
    source_device: int | None
    source_inode: int | None
    available_bytes: int

    def __post_init__(self) -> None:
        if not self.torrent_path or self.expected_length < 0 or self.available_bytes < 0:
            raise ValueError("repair target evidence 基础字段无效")
        target_fields = (
            self.target_device,
            self.target_inode,
            self.target_size,
            self.target_link_count,
        )
        target_identity_complete = all(value is not None for value in target_fields)
        target_identity_empty = all(value is None for value in target_fields)
        if (self.target_exists and not target_identity_complete) or (
            not self.target_exists and not target_identity_empty
        ):
            raise ValueError("repair target evidence 的 target 身份字段不完整")
        if any(value is not None and value < 0 for value in target_fields):
            raise ValueError("repair target evidence 的 target 身份字段不能为负数")
        if self.target_link_count is not None and self.target_link_count < 1:
            raise ValueError("repair target evidence 的 link count 必须大于 0")
        if (self.source_device is None) != (self.source_inode is None):
            raise ValueError("repair target evidence 的 source 身份字段不完整")
        if self.source_device is not None and (self.source_device < 0 or self.source_inode is None):
            raise ValueError("repair target evidence 的 source 身份字段无效")
        if self.source_inode is not None and self.source_inode < 0:
            raise ValueError("repair target evidence 的 source inode 无效")

    @property
    def shares_source_inode(self) -> bool | None:
        if self.source_device is None or self.source_inode is None or not self.target_exists:
            return None
        assert self.target_device is not None and self.target_inode is not None
        return self.target_device == self.source_device and self.target_inode == self.source_inode


@dataclass(frozen=True, slots=True)
class RepairPieceRef:
    scope: RepairPieceScope
    index: int
    status: PieceStatus
    covered_files: tuple[str, ...]
    torrent_path: str | None = None

    def __post_init__(self) -> None:
        if self.index < 0 or self.status is PieceStatus.VERIFIED or not self.covered_files:
            raise ValueError("repair piece ref 必须指向非 VERIFIED 的有效 piece")
        if self.scope is RepairPieceScope.V1_STREAM and self.torrent_path is not None:
            raise ValueError("v1 stream piece 不绑定单文件 torrent_path")
        if self.scope is RepairPieceScope.V2_FILE and not self.torrent_path:
            raise ValueError("v2 file piece 必须绑定 torrent_path")


@dataclass(frozen=True, slots=True)
class RepairAffectedFile:
    torrent_path: str
    length: int
    mapping_state: FileMappingState
    affected_pieces: tuple[RepairPieceRef, ...]
    target_exists: bool | None
    shares_source_inode: bool | None
    isolation_required: bool
    whole_file_fetch: bool


@dataclass(frozen=True, slots=True)
class RepairAction:
    kind: RepairActionKind
    torrent_path: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class RepairPlan:
    mode: RepairMode
    torrent_kind: TorrentKind
    affected_pieces: tuple[RepairPieceRef, ...]
    cross_file_pieces: tuple[RepairPieceRef, ...]
    affected_files: tuple[RepairAffectedFile, ...]
    actions: tuple[RepairAction, ...]
    isolation_bytes_required: int
    estimated_download_bytes_upper_bound: int
    required_free_bytes: int
    available_bytes: int
    downloader_paused: bool
    blocked_reasons: tuple[RepairBlockReason, ...]
    ready: bool
    execution_allowed: bool = False


def build_repair_plan(
    meta: TorrentMeta,
    verification: TorrentVerificationResult,
    target_evidence: tuple[RepairTargetEvidence, ...],
    *,
    mode: RepairMode,
    downloader_paused: bool,
) -> RepairPlan:
    """根据只读验证和文件系统证据生成 99% 修复计划；本函数永远不授权写入。"""

    _assert_verification_kind(meta, verification)
    mapping_by_path = _mapping_index(verification)
    target_by_path = _target_index(target_evidence)
    file_by_path = {item.path: item for item in meta.files}
    piece_refs = _affected_piece_refs(verification)
    affected_paths = {
        path
        for piece in piece_refs
        for path in piece.covered_files
        if path in file_by_path
        and not file_by_path[path].padding
        and not file_by_path[path].zero_length
    }
    for mapping_evidence in mapping_by_path.values():
        if mapping_evidence.state in {FileMappingState.MISSING, FileMappingState.AMBIGUOUS}:
            torrent_file = file_by_path.get(mapping_evidence.torrent_path)
            if (
                torrent_file is not None
                and not torrent_file.padding
                and not torrent_file.zero_length
            ):
                affected_paths.add(mapping_evidence.torrent_path)

    cross_file_pieces = tuple(
        piece
        for piece in piece_refs
        if piece.scope is RepairPieceScope.V1_STREAM
        and len(
            {
                path
                for path in piece.covered_files
                if path in file_by_path
                and not file_by_path[path].padding
                and not file_by_path[path].zero_length
            }
        )
        > 1
    )

    blockers: set[RepairBlockReason] = set()
    if _verification_level(verification) is VerificationLevel.BLOCKED:
        blockers.add(RepairBlockReason.VERIFICATION_BLOCKED)
    if not affected_paths:
        blockers.add(RepairBlockReason.NO_REPAIR_NEEDED)

    affected_files: list[RepairAffectedFile] = []
    isolation_bytes = 0
    missing_target_bytes = 0
    available_values: list[int] = []
    for torrent_file in meta.files:
        if torrent_file.path not in affected_paths:
            continue
        mapping = mapping_by_path.get(torrent_file.path)
        mapping_state = mapping.state if mapping is not None else FileMappingState.MISSING
        if mapping_state is FileMappingState.AMBIGUOUS:
            blockers.add(RepairBlockReason.VERIFICATION_BLOCKED)
        target = target_by_path.get(torrent_file.path)
        target_exists: bool | None = None
        shares_source_inode: bool | None = None
        isolation_required = False
        whole_file_fetch = mapping_state is FileMappingState.MISSING
        if target is None:
            blockers.add(RepairBlockReason.TARGET_EVIDENCE_MISSING)
        else:
            available_values.append(target.available_bytes)
            if target.expected_length != torrent_file.length:
                raise ValueError("repair target evidence 与 torrent file 长度不一致")
            target_exists = target.target_exists
            shares_source_inode = target.shares_source_inode
            if target.target_exists and target.target_size != torrent_file.length:
                blockers.add(RepairBlockReason.TARGET_LENGTH_MISMATCH)
            if target.target_exists and target.target_link_count is not None:
                isolation_required = target.target_link_count > 1
            if mapping_state is FileMappingState.MAPPED:
                if not target.target_exists:
                    blockers.add(RepairBlockReason.TARGET_MISSING)
                elif shares_source_inode is None:
                    blockers.add(RepairBlockReason.TARGET_SOURCE_IDENTITY_UNKNOWN)
                elif shares_source_inode:
                    isolation_required = True
            elif not target.target_exists:
                missing_target_bytes += torrent_file.length
            if isolation_required:
                isolation_bytes += torrent_file.length

        file_pieces = tuple(
            piece for piece in piece_refs if torrent_file.path in piece.covered_files
        )
        affected_files.append(
            RepairAffectedFile(
                torrent_path=torrent_file.path,
                length=torrent_file.length,
                mapping_state=mapping_state,
                affected_pieces=file_pieces,
                target_exists=target_exists,
                shares_source_inode=shares_source_inode,
                isolation_required=isolation_required,
                whole_file_fetch=whole_file_fetch,
            )
        )

    available_bytes = min(available_values) if available_values else 0
    required_free_bytes = isolation_bytes + missing_target_bytes
    if mode is not RepairMode.GUIDED:
        if not downloader_paused:
            blockers.add(RepairBlockReason.DOWNLOADER_NOT_PAUSED)
        if available_values and available_bytes < required_free_bytes:
            blockers.add(RepairBlockReason.INSUFFICIENT_SPACE)
    if mode is RepairMode.FILE_ONLY:
        if cross_file_pieces:
            blockers.add(RepairBlockReason.FILE_ONLY_CROSS_FILE_PIECE)
        if any(item.isolation_required for item in affected_files):
            blockers.add(RepairBlockReason.FILE_ONLY_REQUIRES_ISOLATION)

    actions = _build_actions(mode, tuple(affected_files), piece_refs)
    estimated_download = sum(item.length for item in affected_files)
    normalized_blockers = tuple(sorted(blockers, key=lambda item: item.value))
    return RepairPlan(
        mode=mode,
        torrent_kind=meta.torrent_kind,
        affected_pieces=piece_refs,
        cross_file_pieces=cross_file_pieces,
        affected_files=tuple(affected_files),
        actions=actions,
        isolation_bytes_required=isolation_bytes,
        estimated_download_bytes_upper_bound=estimated_download,
        required_free_bytes=required_free_bytes,
        available_bytes=available_bytes,
        downloader_paused=downloader_paused,
        blocked_reasons=normalized_blockers,
        ready=not normalized_blockers,
        execution_allowed=False,
    )


def _mapping_index(
    verification: TorrentVerificationResult,
) -> dict[str, FileMappingEvidence]:
    mappings = (
        verification.v1.mappings
        if isinstance(verification, HybridVerificationResult)
        else verification.mappings
    )
    indexed: dict[str, FileMappingEvidence] = {}
    for mapping in mappings:
        if mapping.torrent_path in indexed:
            raise ValueError("repair verification 包含重复 mapping")
        indexed[mapping.torrent_path] = mapping
    return indexed


def _target_index(targets: tuple[RepairTargetEvidence, ...]) -> dict[str, RepairTargetEvidence]:
    indexed: dict[str, RepairTargetEvidence] = {}
    for target in targets:
        if target.torrent_path in indexed:
            raise ValueError("repair target evidence 包含重复 torrent_path")
        indexed[target.torrent_path] = target
    return indexed


def _affected_piece_refs(verification: TorrentVerificationResult) -> tuple[RepairPieceRef, ...]:
    refs: list[RepairPieceRef] = []
    if isinstance(verification, V1VerificationResult):
        refs.extend(_v1_piece_refs(verification.pieces))
    elif isinstance(verification, V2VerificationResult):
        refs.extend(_v2_piece_refs(verification))
    else:
        refs.extend(_v1_piece_refs(verification.v1.pieces))
        refs.extend(_v2_piece_refs(verification.v2))
    return tuple(refs)


def _v1_piece_refs(pieces: tuple[PieceEvidence, ...]) -> list[RepairPieceRef]:
    return [
        RepairPieceRef(
            scope=RepairPieceScope.V1_STREAM,
            index=piece.index,
            status=piece.status,
            covered_files=piece.covered_files,
        )
        for piece in pieces
        if piece.status is not PieceStatus.VERIFIED and piece.covered_files
    ]


def _v2_piece_refs(result: V2VerificationResult) -> list[RepairPieceRef]:
    refs: list[RepairPieceRef] = []
    for file_result in result.files:
        for piece in file_result.pieces:
            if piece.status is PieceStatus.VERIFIED:
                continue
            refs.append(
                RepairPieceRef(
                    scope=RepairPieceScope.V2_FILE,
                    index=piece.index,
                    status=piece.status,
                    covered_files=(file_result.torrent_path,),
                    torrent_path=file_result.torrent_path,
                )
            )
    return refs


def _build_actions(
    mode: RepairMode,
    files: tuple[RepairAffectedFile, ...],
    pieces: tuple[RepairPieceRef, ...],
) -> tuple[RepairAction, ...]:
    if mode is RepairMode.GUIDED:
        return (
            RepairAction(
                RepairActionKind.MANUAL_GUIDANCE,
                None,
                "仅导出受影响 piece、文件、inode 隔离与空间前置条件，不执行任何写操作。",
            ),
        )

    actions: list[RepairAction] = []
    for item in files:
        if item.isolation_required:
            actions.append(
                RepairAction(
                    RepairActionKind.ISOLATE_TARGET,
                    item.torrent_path,
                    "未来写入前必须复制并原子替换为与源数据不同的 inode。",
                )
            )
        if item.whole_file_fetch:
            actions.append(
                RepairAction(
                    RepairActionKind.FETCH_FILE,
                    item.torrent_path,
                    "源目录缺少该文件，未来只能在目标侧补齐，禁止写回源目录。",
                )
            )
        elif mode is RepairMode.FILE_ONLY:
            actions.append(
                RepairAction(
                    RepairActionKind.REPAIR_FILE,
                    item.torrent_path,
                    "仅允许对已证明独立 inode 的目标文件执行文件级修复。",
                )
            )
    if mode is RepairMode.AUTO_PIECE and pieces:
        actions.append(
            RepairAction(
                RepairActionKind.REPAIR_PIECES,
                None,
                "完成全部必要 inode 隔离后，才允许未来按受影响 piece 触发补齐与重校验。",
            )
        )
    return tuple(actions)


def _verification_level(verification: TorrentVerificationResult) -> VerificationLevel:
    return verification.level


def _assert_verification_kind(meta: TorrentMeta, verification: TorrentVerificationResult) -> None:
    if isinstance(verification, HybridVerificationResult):
        if meta.torrent_kind is not TorrentKind.HYBRID:
            raise ValueError("hybrid verification 只能用于 hybrid torrent")
        return
    if isinstance(verification, V1VerificationResult):
        if meta.torrent_kind not in {TorrentKind.V1, TorrentKind.HYBRID}:
            raise ValueError("v1 verification 与 torrent kind 不匹配")
        return
    if meta.torrent_kind not in {TorrentKind.V2, TorrentKind.HYBRID}:
        raise ValueError("v2 verification 与 torrent kind 不匹配")
