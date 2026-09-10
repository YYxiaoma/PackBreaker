from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from backend.app.application.analysis import verify_torrent_mappings
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.file_mapping import (
    AutoMappingDecision,
    MappingMethod,
    auto_map_files,
)
from backend.app.domain.verification import FileMappingState, VerificationLevel
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)
from backend.app.infrastructure.torrent_parser import BencodeLimits, parse_torrent

M2_CORPUS_ACCEPTANCE_SCHEMA_VERSION = "packbreaker-m2-corpus-acceptance-v1"


@dataclass(frozen=True, slots=True)
class M2CorpusAcceptanceReport:
    torrent_kind: str
    private: bool
    torrent_file_count: int
    torrent_total_bytes: int
    piece_length: int
    v1_piece_count: int
    v2_piece_hash_count: int
    source_file_count: int
    source_total_bytes: int
    repeated_runs: int
    source_inventory_digest: str
    mapping_digest: str
    mapping_state_counts: tuple[tuple[str, int], ...]
    mapping_method_counts: tuple[tuple[str, int], ...]
    inventory_stable: bool
    mapping_stable: bool
    verification_performed: bool
    verification_level: VerificationLevel | None
    source_unchanged_after_verification: bool | None
    status: str
    schema_version: str = M2_CORPUS_ACCEPTANCE_SCHEMA_VERSION
    execution_allowed: bool = False
    side_effects_started: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "torrent": {
                "kind": self.torrent_kind,
                "private": self.private,
                "file_count": self.torrent_file_count,
                "total_bytes": self.torrent_total_bytes,
                "piece_length": self.piece_length,
                "v1_piece_count": self.v1_piece_count,
                "v2_piece_hash_count": self.v2_piece_hash_count,
            },
            "source": {
                "file_count": self.source_file_count,
                "total_bytes": self.source_total_bytes,
                "inventory_digest": self.source_inventory_digest,
            },
            "mapping": {
                "digest": self.mapping_digest,
                "state_counts": dict(self.mapping_state_counts),
                "method_counts": dict(self.mapping_method_counts),
            },
            "stability": {
                "repeated_runs": self.repeated_runs,
                "inventory_stable": self.inventory_stable,
                "mapping_stable": self.mapping_stable,
                "source_unchanged_after_verification": self.source_unchanged_after_verification,
            },
            "verification": {
                "performed": self.verification_performed,
                "level": self.verification_level.value
                if self.verification_level is not None
                else None,
            },
            "execution_allowed": self.execution_allowed,
            "side_effects_started": self.side_effects_started,
        }


def run_m2_corpus_acceptance(
    *,
    torrent_path: Path,
    source_root: Path,
    repeated_runs: int = 3,
    max_files: int = 100_000,
    verify_content: bool = False,
) -> M2CorpusAcceptanceReport:
    """只读运行真实语料结构、映射和可选内容验证，不产生任何执行副作用。"""

    if repeated_runs < 2:
        raise ValueError("repeated_runs 必须至少为 2")
    if max_files <= 0:
        raise ValueError("max_files 必须大于 0")

    meta = parse_torrent(_read_torrent_safely(torrent_path))
    inventories = tuple(
        scan_source_inventory(source_root, max_files=max_files) for _ in range(repeated_runs)
    )
    inventory_digests = tuple(source_inventory_digest(inventory) for inventory in inventories)
    mappings = tuple(auto_map_files(meta, inventory) for inventory in inventories)
    mapping_digests = tuple(_mapping_digest(items, source_root) for items in mappings)

    inventory_stable = len(set(inventory_digests)) == 1
    mapping_stable = len(set(mapping_digests)) == 1
    verification_level: VerificationLevel | None = None
    source_unchanged_after_verification: bool | None = None
    status = "PASS"

    if not inventory_stable or not mapping_stable:
        status = "SOURCE_CHANGED_DURING_ACCEPTANCE"
    elif verify_content:
        verification_level = verify_torrent_mappings(meta, mappings[0])
        final_inventory = scan_source_inventory(source_root, max_files=max_files)
        source_unchanged_after_verification = (
            source_inventory_digest(final_inventory) == inventory_digests[0]
        )
        if not source_unchanged_after_verification:
            status = "SOURCE_CHANGED_AFTER_VERIFICATION"

    first_inventory = inventories[0]
    first_mappings = mappings[0]
    return M2CorpusAcceptanceReport(
        torrent_kind=meta.torrent_kind.value,
        private=meta.private,
        torrent_file_count=len(meta.files),
        torrent_total_bytes=sum(item.length for item in meta.files),
        piece_length=meta.piece_length,
        v1_piece_count=len(meta.v1_piece_hashes),
        v2_piece_hash_count=sum(len(item.hashes) for item in meta.v2_piece_layers),
        source_file_count=len(first_inventory),
        source_total_bytes=sum(item.length for item in first_inventory),
        repeated_runs=repeated_runs,
        source_inventory_digest=inventory_digests[0],
        mapping_digest=mapping_digests[0],
        mapping_state_counts=_enum_counts(first_mappings, FileMappingState),
        mapping_method_counts=_enum_counts(first_mappings, MappingMethod),
        inventory_stable=inventory_stable,
        mapping_stable=mapping_stable,
        verification_performed=verify_content and inventory_stable and mapping_stable,
        verification_level=verification_level,
        source_unchanged_after_verification=source_unchanged_after_verification,
        status=status,
    )


def _read_torrent_safely(path: Path) -> bytes:
    limits = BencodeLimits()
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取 torrent 文件状态") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "torrent 输入必须是普通文件且不能是符号链接"
        )
    if before.st_size > limits.max_payload_bytes:
        raise DomainViolation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "torrent payload 超过解析上限")

    try:
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取 torrent 文件") from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "读取期间 torrent 文件发生变化")
    return payload


def _mapping_digest(mappings: tuple[AutoMappingDecision, ...], source_root: Path) -> str:
    payload = [
        {
            "torrent_path": item.torrent_path,
            "state": item.state.value,
            "method": item.method.value,
            "source": _relative_source(item.source_path, source_root),
            "candidates": sorted(
                _relative_required_source(candidate, source_root)
                for candidate in item.candidate_paths
            ),
        }
        for item in mappings
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


def _relative_source(value: str | None, source_root: Path) -> str | None:
    if value is None:
        return None
    try:
        return Path(value).relative_to(source_root).as_posix()
    except ValueError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射源文件逃逸验收根目录") from exc


def _relative_required_source(value: str, source_root: Path) -> str:
    result = _relative_source(value, source_root)
    if result is None:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "候选源路径不能为空")
    return result


def _enum_counts(
    mappings: tuple[AutoMappingDecision, ...],
    enum_type: type[FileMappingState] | type[MappingMethod],
) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {item.value: 0 for item in enum_type}
    for mapping in mappings:
        value = mapping.state.value if enum_type is FileMappingState else mapping.method.value
        counts[value] += 1
    return tuple(counts.items())
