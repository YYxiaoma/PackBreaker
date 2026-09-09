from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from backend.app.domain.candidate_scoring import CandidateScore
from backend.app.domain.file_mapping import AutoMappingDecision
from backend.app.domain.site_search import SearchQuery
from backend.app.domain.verification import VerificationLevel

PREFLIGHT_SCHEMA_VERSION = "packbreaker-preflight-v1"


@dataclass(frozen=True, slots=True)
class SiteAnalysisEvidence:
    site_config_id: str
    site_config_version: int
    site_id: str
    executed_queries: tuple[str, ...]
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidatePreflightEvidence:
    site_id: str
    torrent_id: str
    display_name: str
    score: CandidateScore
    selected_for_verification: bool
    metainfo_digest: str | None
    verification_level: VerificationLevel | None
    mappings: tuple[AutoMappingDecision, ...]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PreflightSnapshot:
    task_id: str
    task_version: int
    normalized_unit_key: str
    source_inventory_digest: str
    site_versions: tuple[tuple[str, int], ...]
    planned_queries: tuple[str, ...]
    sites: tuple[SiteAnalysisEvidence, ...]
    candidates: tuple[CandidatePreflightEvidence, ...]
    created_at: datetime
    schema_version: str = PREFLIGHT_SCHEMA_VERSION
    snapshot_digest: str = ""

    def __post_init__(self) -> None:
        if not self.task_id or not self.normalized_unit_key:
            raise ValueError("preflight 必须绑定 task 与 unit")
        if self.task_version < 1:
            raise ValueError("preflight task_version 必须大于等于 1")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("preflight 创建时间必须带时区")
        normalized_time = self.created_at.astimezone(UTC)
        object.__setattr__(self, "created_at", normalized_time)
        ordered_versions = tuple(sorted(self.site_versions))
        object.__setattr__(self, "site_versions", ordered_versions)
        digest_payload = _snapshot_payload(self, include_digest=False)
        digest_payload.pop("created_at", None)
        digest = sha256(_canonical_json(digest_payload)).hexdigest()
        if self.snapshot_digest and self.snapshot_digest != digest:
            raise ValueError("preflight snapshot digest 与内容不一致")
        object.__setattr__(self, "snapshot_digest", digest)

    def is_current(
        self,
        *,
        task_version: int,
        source_inventory_digest: str,
        site_versions: tuple[tuple[str, int], ...],
    ) -> bool:
        return (
            self.task_version == task_version
            and self.source_inventory_digest == source_inventory_digest
            and self.site_versions == tuple(sorted(site_versions))
        )


def snapshot_to_payload(snapshot: PreflightSnapshot) -> dict[str, object]:
    return _snapshot_payload(snapshot, include_digest=True)


def search_query_signature(query: SearchQuery) -> str:
    external_ids = ",".join(f"{item.namespace}:{item.value}" for item in query.external_ids)
    episode = repr(query.episode) if query.episode is not None else ""
    return "|".join(
        (
            query.media_type.value,
            query.query_text,
            episode,
            external_ids,
            str(query.page),
            str(query.page_size),
            query.sort.value,
        )
    )


def _snapshot_payload(snapshot: PreflightSnapshot, *, include_digest: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": snapshot.schema_version,
        "task_id": snapshot.task_id,
        "task_version": snapshot.task_version,
        "normalized_unit_key": snapshot.normalized_unit_key,
        "source_inventory_digest": snapshot.source_inventory_digest,
        "site_versions": [[site_id, version] for site_id, version in snapshot.site_versions],
        "planned_queries": list(snapshot.planned_queries),
        "sites": [
            {
                "site_config_id": item.site_config_id,
                "site_config_version": item.site_config_version,
                "site_id": item.site_id,
                "executed_queries": list(item.executed_queries),
                "error_codes": list(item.error_codes),
            }
            for item in snapshot.sites
        ],
        "candidates": [_candidate_payload(item) for item in snapshot.candidates],
        "created_at": snapshot.created_at.isoformat(),
    }
    if include_digest:
        payload["snapshot_digest"] = snapshot.snapshot_digest
    return payload


def _candidate_payload(item: CandidatePreflightEvidence) -> dict[str, object]:
    return {
        "site_id": item.site_id,
        "torrent_id": item.torrent_id,
        "display_name": item.display_name,
        "score": {
            "score": item.score.score,
            "rejected": item.score.rejected,
            "hard_conflicts": [value.value for value in item.score.hard_conflicts],
            "dimensions": [
                {
                    "dimension": dimension.dimension.value,
                    "earned": dimension.earned,
                    "maximum": dimension.maximum,
                    "evidence": dimension.evidence,
                }
                for dimension in item.score.dimensions
            ],
            "algorithm_version": item.score.algorithm_version,
            "config_version": item.score.config_version,
            "automatic_action_allowed": item.score.automatic_action_allowed,
        },
        "selected_for_verification": item.selected_for_verification,
        "metainfo_digest": item.metainfo_digest,
        "verification_level": (
            item.verification_level.value if item.verification_level is not None else None
        ),
        "mappings": [
            {
                "torrent_path": mapping.torrent_path,
                "state": mapping.state.value,
                "method": mapping.method.value,
                "source_path": mapping.source_path,
                "snapshot": (
                    {
                        "device": mapping.snapshot.device,
                        "inode": mapping.snapshot.inode,
                        "size": mapping.snapshot.size,
                        "mtime_ns": mapping.snapshot.mtime_ns,
                        "file_type": mapping.snapshot.file_type,
                    }
                    if mapping.snapshot is not None
                    else None
                ),
                "candidate_paths": list(mapping.candidate_paths),
            }
            for mapping in item.mappings
        ],
        "error_code": item.error_code,
    }


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
