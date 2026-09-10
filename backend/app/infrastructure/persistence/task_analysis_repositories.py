from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.domain.execution_gate import ExecutionGateSnapshot, execution_gate_to_payload
from backend.app.domain.execution_plan import ExecutionPlanSnapshot, execution_plan_to_payload
from backend.app.domain.preflight import PreflightSnapshot, candidate_evidence_to_payload
from backend.app.domain.review import (
    ReviewState,
    ReviewVerificationSnapshot,
    review_verification_to_payload,
)
from backend.app.domain.task_units import TaskUnit
from backend.app.infrastructure.persistence.models import (
    PreflightSnapshotRecord,
    TaskCandidateRecord,
    TaskExecutionGateRecord,
    TaskExecutionPlanRecord,
    TaskReviewRevisionRecord,
    TaskReviewVerificationRecord,
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

    def get(self, unit_id: str) -> TaskUnitRecord | None:
        return self._session.get(TaskUnitRecord, unit_id)

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

    def list_for_snapshot(self, snapshot_id: str) -> list[TaskCandidateRecord]:
        return list(
            self._session.scalars(
                select(TaskCandidateRecord)
                .where(TaskCandidateRecord.preflight_snapshot_id == snapshot_id)
                .order_by(
                    TaskCandidateRecord.rejected,
                    TaskCandidateRecord.score.desc(),
                    TaskCandidateRecord.site_id,
                    TaskCandidateRecord.torrent_id,
                )
            )
        )

    def get(self, candidate_id: str) -> TaskCandidateRecord | None:
        return self._session.get(TaskCandidateRecord, candidate_id)


class TaskReviewRepository:
    """审核 revision 只追加；并发通过 expected_version + 唯一约束失败关闭。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest(
        self,
        *,
        task_unit_id: str,
        preflight_snapshot_id: str,
    ) -> TaskReviewRevisionRecord | None:
        return self._session.scalar(
            select(TaskReviewRevisionRecord)
            .where(
                TaskReviewRevisionRecord.task_unit_id == task_unit_id,
                TaskReviewRevisionRecord.preflight_snapshot_id == preflight_snapshot_id,
            )
            .order_by(TaskReviewRevisionRecord.version.desc())
            .limit(1)
        )

    def append(
        self,
        *,
        task_id: str,
        task_unit_id: str,
        preflight_snapshot_id: str,
        expected_version: int,
        state: ReviewState,
        requires_reverification: bool,
        actor_kind: str,
        actor_id: str,
    ) -> TaskReviewRevisionRecord:
        latest = self.latest(
            task_unit_id=task_unit_id,
            preflight_snapshot_id=preflight_snapshot_id,
        )
        current_version = latest.version if latest is not None else 0
        if current_version != expected_version:
            raise ValueError("REVIEW_VERSION_CONFLICT")
        record = TaskReviewRevisionRecord(
            id=new_uuid(),
            task_id=task_id,
            task_unit_id=task_unit_id,
            preflight_snapshot_id=preflight_snapshot_id,
            approved_candidate_id=state.approved_candidate_id,
            rejected_candidate_ids=list(state.rejected_candidate_ids),
            manual_mappings=[
                {
                    "torrent_path": item.torrent_path,
                    "source_relative_path": item.source_relative_path,
                }
                for item in state.manual_mappings
            ],
            note=state.note,
            requires_reverification=requires_reverification,
            actor_kind=actor_kind,
            actor_id=actor_id,
            version=current_version + 1,
            created_at=utc_now(),
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError as exc:
            raise ValueError("REVIEW_VERSION_CONFLICT") from exc
        return record


class TaskReviewVerificationRepository:
    """人工映射重验证证据只追加；同一审核 revision 最多保存一份结果。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_revision(self, review_revision_id: str) -> TaskReviewVerificationRecord | None:
        return self._session.scalar(
            select(TaskReviewVerificationRecord).where(
                TaskReviewVerificationRecord.review_revision_id == review_revision_id
            )
        )

    def create_or_get(
        self,
        snapshot: ReviewVerificationSnapshot,
    ) -> tuple[TaskReviewVerificationRecord, bool]:
        existing = self.get_for_revision(snapshot.review_revision_id)
        if existing is not None:
            if existing.verification_digest != snapshot.verification_digest:
                raise ValueError("REVIEW_VERIFICATION_CONFLICT")
            return existing, False
        payload = review_verification_to_payload(snapshot)
        record = TaskReviewVerificationRecord(
            id=new_uuid(),
            review_revision_id=snapshot.review_revision_id,
            review_version=snapshot.review_version,
            task_id=snapshot.task_id,
            task_unit_id=snapshot.task_unit_id,
            preflight_snapshot_id=snapshot.preflight_snapshot_id,
            candidate_id=snapshot.candidate_id,
            source_inventory_digest=snapshot.source_inventory_digest,
            metainfo_digest=snapshot.metainfo_digest,
            verification_level=snapshot.verification_level.value,
            mappings=deepcopy(payload["mappings"]),
            verification_digest=snapshot.verification_digest,
            created_at=snapshot.created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError as exc:
            concurrent = self.get_for_revision(snapshot.review_revision_id)
            if concurrent is None or concurrent.verification_digest != snapshot.verification_digest:
                raise ValueError("REVIEW_VERIFICATION_CONFLICT") from exc
            return concurrent, False
        return record, True


class TaskExecutionGateRepository:
    """执行门证据只追加；gate digest 相同则幂等复用。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest(self, task_unit_id: str) -> TaskExecutionGateRecord | None:
        return self._session.scalar(
            select(TaskExecutionGateRecord)
            .where(TaskExecutionGateRecord.task_unit_id == task_unit_id)
            .order_by(TaskExecutionGateRecord.created_at.desc(), TaskExecutionGateRecord.id.desc())
            .limit(1)
        )

    def get_by_digest(self, gate_digest: str) -> TaskExecutionGateRecord | None:
        return self._session.scalar(
            select(TaskExecutionGateRecord).where(
                TaskExecutionGateRecord.gate_digest == gate_digest
            )
        )

    def create_or_get(
        self,
        snapshot: ExecutionGateSnapshot,
    ) -> tuple[TaskExecutionGateRecord, bool]:
        existing = self.get_by_digest(snapshot.gate_digest)
        if existing is not None:
            return existing, False
        payload = execution_gate_to_payload(snapshot)
        record = TaskExecutionGateRecord(
            id=new_uuid(),
            task_id=snapshot.task_id,
            task_unit_id=snapshot.task_unit_id,
            preflight_snapshot_id=snapshot.preflight_snapshot_id,
            review_revision_id=snapshot.review_revision_id,
            candidate_id=snapshot.candidate_id,
            review_verification_id=snapshot.review_verification_id,
            task_version=snapshot.task_version,
            eligible=snapshot.eligible,
            client_check_required=snapshot.client_check_required,
            verification_level=(
                snapshot.verification_level.value
                if snapshot.verification_level is not None
                else None
            ),
            metainfo_digest=snapshot.metainfo_digest,
            blocked_reasons=[item.value for item in snapshot.blocked_reasons],
            gate_digest=snapshot.gate_digest,
            payload=deepcopy(payload),
            created_at=snapshot.created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            concurrent = self.get_by_digest(snapshot.gate_digest)
            if concurrent is None:
                raise
            return concurrent, False
        return record, True


class TaskExecutionPlanRepository:
    """执行计划只追加；相同 plan digest 幂等复用。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, plan_id: str) -> TaskExecutionPlanRecord | None:
        return self._session.get(TaskExecutionPlanRecord, plan_id)

    def latest(self, task_unit_id: str) -> TaskExecutionPlanRecord | None:
        return self._session.scalar(
            select(TaskExecutionPlanRecord)
            .where(TaskExecutionPlanRecord.task_unit_id == task_unit_id)
            .order_by(TaskExecutionPlanRecord.created_at.desc(), TaskExecutionPlanRecord.id.desc())
            .limit(1)
        )

    def get_by_digest(self, plan_digest: str) -> TaskExecutionPlanRecord | None:
        return self._session.scalar(
            select(TaskExecutionPlanRecord).where(
                TaskExecutionPlanRecord.plan_digest == plan_digest
            )
        )

    def create_or_get(
        self,
        snapshot: ExecutionPlanSnapshot,
    ) -> tuple[TaskExecutionPlanRecord, bool]:
        existing = self.get_by_digest(snapshot.plan_digest)
        if existing is not None:
            return existing, False
        payload = execution_plan_to_payload(snapshot)
        record = TaskExecutionPlanRecord(
            id=new_uuid(),
            task_id=snapshot.task_id,
            task_unit_id=snapshot.task_unit_id,
            execution_gate_id=snapshot.execution_gate_id,
            candidate_id=snapshot.candidate_id,
            task_version=snapshot.task_version,
            target_root=snapshot.target_root,
            target_device=snapshot.target_device,
            verification_level=snapshot.verification_level.value,
            client_check_required=snapshot.client_check_required,
            ready=snapshot.ready,
            blocked_reasons=[item.value for item in snapshot.blocked_reasons],
            estimated_download_bytes_upper_bound=snapshot.estimated_download_bytes_upper_bound,
            plan_digest=snapshot.plan_digest,
            payload=deepcopy(payload),
            created_at=snapshot.created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            concurrent = self.get_by_digest(snapshot.plan_digest)
            if concurrent is None:
                raise
            return concurrent, False
        return record, True
