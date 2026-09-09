from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.domain.preflight import PreflightSnapshot, snapshot_to_payload
from backend.app.infrastructure.persistence.models import PreflightSnapshotRecord, new_uuid


class PreflightSnapshotRepository:
    """只追加 preflight；不提供 update/delete，避免历史证据被原位改写。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_digest(self, snapshot_digest: str) -> PreflightSnapshotRecord | None:
        return self._session.scalar(
            select(PreflightSnapshotRecord).where(
                PreflightSnapshotRecord.snapshot_digest == snapshot_digest
            )
        )

    def latest_for_task(self, task_id: str) -> PreflightSnapshotRecord | None:
        return self._session.scalar(
            select(PreflightSnapshotRecord)
            .where(PreflightSnapshotRecord.task_id == task_id)
            .order_by(PreflightSnapshotRecord.created_at.desc(), PreflightSnapshotRecord.id.desc())
            .limit(1)
        )

    def create_or_get(self, snapshot: PreflightSnapshot) -> tuple[PreflightSnapshotRecord, bool]:
        existing = self.get_by_digest(snapshot.snapshot_digest)
        if existing is not None:
            return existing, False
        record = PreflightSnapshotRecord(
            id=new_uuid(),
            task_id=snapshot.task_id,
            task_version=snapshot.task_version,
            normalized_unit_key=snapshot.normalized_unit_key,
            source_inventory_digest=snapshot.source_inventory_digest,
            snapshot_digest=snapshot.snapshot_digest,
            payload=deepcopy(snapshot_to_payload(snapshot)),
            created_at=snapshot.created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            concurrent = self.get_by_digest(snapshot.snapshot_digest)
            if concurrent is None:
                raise
            return concurrent, False
        return record, True
