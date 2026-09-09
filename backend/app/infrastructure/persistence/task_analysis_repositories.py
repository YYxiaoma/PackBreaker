from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.preflight import PreflightSnapshot, candidate_evidence_to_payload
from backend.app.domain.task_units import TaskUnit
from backend.app.infrastructure.persistence.models import (
    PreflightSnapshotRecord,
    TaskCandidateRecord,
    TaskUnitRecord,
    new_uuid,
    utc_now,
)


class TaskUnitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_batch(
        self,
        *,
        task_id: str,
        source_root: str,
        source_inventory_digest: str,
        units: tuple[TaskUnit, ...],
    ) -> tuple[TaskUnitRecord, ...]:
        existing = {
            item.normalized_unit_key: item
            for item in self._session.scalars(
                select(TaskUnitRecord).where(
                    TaskUnitRecord.task_id == task_id,
                    TaskUnitRecord.source_inventory_digest == source_inventory_digest,
                )
            )
        }
        records: list[TaskUnitRecord] = []
        now = utc_now()
        for unit in units:
            record = existing.get(unit.normalized_unit_key)
            if record is None:
                record = TaskUnitRecord(
                    id=new_uuid(),
                    task_id=task_id,
                    normalized_unit_key=unit.normalized_unit_key,
                    source_root=source_root,
                    source_inventory_digest=source_inventory_digest,
                    kind=unit.kind.value,
                    source_relative_path=unit.source_relative_path,
                    length=unit.length,
                    descriptor=deepcopy(asdict(unit.descriptor)),
                    discovered_at=now,
                )
                self._session.add(record)
            records.append(record)
        self._session.flush()
        return tuple(records)

    def get_for_snapshot(
        self,
        *,
        task_id: str,
        normalized_unit_key: str,
        source_inventory_digest: str,
    ) -> TaskUnitRecord | None:
        return self._session.scalar(
            select(TaskUnitRecord).where(
                TaskUnitRecord.task_id == task_id,
                TaskUnitRecord.normalized_unit_key == normalized_unit_key,
                TaskUnitRecord.source_inventory_digest == source_inventory_digest,
            )
        )

    def list_latest(self, task_id: str) -> list[TaskUnitRecord]:
        latest = self._session.scalar(
            select(TaskUnitRecord)
            .where(TaskUnitRecord.task_id == task_id)
            .order_by(TaskUnitRecord.discovered_at.desc(), TaskUnitRecord.id.desc())
            .limit(1)
        )
        if latest is None:
            return []
        return list(
            self._session.scalars(
                select(TaskUnitRecord)
                .where(
                    TaskUnitRecord.task_id == task_id,
                    TaskUnitRecord.source_inventory_digest == latest.source_inventory_digest,
                )
                .order_by(TaskUnitRecord.source_relative_path, TaskUnitRecord.id)
            )
        )


class TaskCandidateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_snapshot(
        self,
        *,
        snapshot_record: PreflightSnapshotRecord,
        snapshot: PreflightSnapshot,
    ) -> tuple[TaskCandidateRecord, ...]:
        existing = {
            (item.site_id, item.torrent_id): item
            for item in self._session.scalars(
                select(TaskCandidateRecord).where(
                    TaskCandidateRecord.preflight_snapshot_id == snapshot_record.id
                )
            )
        }
        records: list[TaskCandidateRecord] = []
        for candidate in snapshot.candidates:
            identity = (candidate.site_id, candidate.torrent_id)
            record = existing.get(identity)
            if record is None:
                record = TaskCandidateRecord(
                    id=new_uuid(),
                    preflight_snapshot_id=snapshot_record.id,
                    task_id=snapshot.task_id,
                    normalized_unit_key=snapshot.normalized_unit_key,
                    site_id=candidate.site_id,
                    torrent_id=candidate.torrent_id,
                    display_name=candidate.display_name,
                    score=candidate.score.score,
                    rejected=candidate.score.rejected,
                    selected_for_verification=candidate.selected_for_verification,
                    verification_level=(
                        candidate.verification_level.value
                        if candidate.verification_level is not None
                        else None
                    ),
                    metainfo_digest=candidate.metainfo_digest,
                    error_code=candidate.error_code,
                    evidence=deepcopy(candidate_evidence_to_payload(candidate)),
                    created_at=snapshot.created_at,
                )
                self._session.add(record)
            records.append(record)
        self._session.flush()
        return tuple(records)

    def list_for_latest_snapshot(self, task_id: str) -> list[TaskCandidateRecord]:
        latest = self._session.scalar(
            select(PreflightSnapshotRecord)
            .where(PreflightSnapshotRecord.task_id == task_id)
            .order_by(PreflightSnapshotRecord.created_at.desc(), PreflightSnapshotRecord.id.desc())
            .limit(1)
        )
        if latest is None:
            return []
        return list(
            self._session.scalars(
                select(TaskCandidateRecord)
                .where(TaskCandidateRecord.preflight_snapshot_id == latest.id)
                .order_by(
                    TaskCandidateRecord.rejected,
                    TaskCandidateRecord.score.desc(),
                    TaskCandidateRecord.site_id,
                    TaskCandidateRecord.torrent_id,
                )
            )
        )
