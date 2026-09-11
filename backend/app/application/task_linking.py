from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    FilesystemOperationService,
    HardlinkExecutionRequest,
)
from backend.app.application.tasks import ExecutionPlanView
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import (
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    execution_plan_actions_from_payload,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.models import TaskExecutionPlanRecord
from backend.app.infrastructure.persistence.preflight_repositories import (
    PreflightSnapshotRepository,
)
from backend.app.infrastructure.persistence.repositories import TaskRepository
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskCandidateRepository,
    TaskExecutionGateRepository,
    TaskExecutionPlanRepository,
    TaskReviewRepository,
    TaskUnitRepository,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)

LINKING_CHECKPOINT_SCHEMA_VERSION = "packbreaker-linking-checkpoint-v1"
ADDING_CHECKPOINT_SCHEMA_VERSION = "packbreaker-adding-checkpoint-v1"


class CurrentExecutionPlanProvider(Protocol):
    def get_execution_plan(self, unit_id: str) -> ExecutionPlanView: ...


@dataclass(frozen=True, slots=True)
class TaskLinkingResult:
    task_id: str
    task_version: int
    execution_plan_id: str
    execution_plan_digest: str
    linked_file_count: int
    client_fetch_count: int
    hardlink_journal_ids: tuple[str, ...]
    directory_journal_ids: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _AuthorizedPlan:
    id: str
    digest: str
    task_id: str
    task_version_before_linking: int
    linking_task_version: int
    unit_id: str
    gate_id: str
    gate_digest: str
    candidate_id: str
    preflight_snapshot_id: str
    review_revision_id: str
    source_root: str
    source_inventory_digest: str
    target_root: str
    target_downloader_id: str
    target_downloader_version: int
    target_downloader_binding_digest: str
    target_remote_save_path: str
    actions: tuple[ExecutionPlanAction, ...]
    client_check_required: bool


class TaskLinkingCoordinator:
    """把 current execution plan 原子授权为 LINKING，并执行已批准 hardlink 动作。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        plan_provider: CurrentExecutionPlanProvider,
        filesystem_operations: FilesystemOperationService,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._plan_provider = plan_provider
        self._filesystem_operations = filesystem_operations
        self._data_root = data_root

    def execute(self, unit_id: str, *, execution_plan_id: str) -> TaskLinkingResult:
        status = self._load_task_status(unit_id, execution_plan_id)
        if status is TaskStatus.AWAITING_CONFIRMATION:
            current = self._plan_provider.get_execution_plan(unit_id)
            if (
                current.id != execution_plan_id
                or not current.current
                or not current.ready
                or current.blocked_reasons
            ):
                raise _linking_plan_not_current()
            plan = self._reserve_linking(unit_id, current)
            replayed = False
        elif status is TaskStatus.LINKING:
            plan = self._load_reserved_plan(unit_id, execution_plan_id)
            replayed = True
        elif status is TaskStatus.ADDING:
            return self._load_completed_linking_result(unit_id, execution_plan_id)
        else:
            raise ApplicationError(
                code="LINKING_TASK_STATE_INVALID",
                status=409,
                title="任务状态不允许执行链接",
                detail="文件系统执行只能从 AWAITING_CONFIRMATION 开始，或恢复已授权的 LINKING",
            )

        self._assert_source_inventory_current(plan)
        hardlink_journal_ids: list[str] = []
        directory_journal_ids: list[str] = []
        client_fetch_count = 0

        for action in plan.actions:
            if action.kind is ExecutionPlanActionKind.CLIENT_FETCH:
                client_fetch_count += 1
                continue
            if action.kind is not ExecutionPlanActionKind.HARDLINK:
                continue
            if action.source_relative_path is None or action.source_snapshot is None:
                raise _linking_plan_invalid("HARDLINK 动作缺少源路径或源快照")
            result = self._filesystem_operations.execute_hardlink(
                HardlinkExecutionRequest(
                    task_id=plan.task_id,
                    candidate_key=plan.digest,
                    source_relative_path=_join_relative_root(
                        plan.source_root,
                        action.source_relative_path,
                    ),
                    target_root_relative_path=plan.target_root,
                    target_relative_path=action.torrent_path,
                    expected_source_snapshot=action.source_snapshot,
                )
            )
            hardlink_journal_ids.append(result.hardlink_journal_id)
            directory_journal_ids.extend(result.directory_journal_ids)

        task_version = self._advance_to_adding(
            plan,
            hardlink_journal_ids=tuple(hardlink_journal_ids),
            directory_journal_ids=tuple(dict.fromkeys(directory_journal_ids)),
            client_fetch_count=client_fetch_count,
        )
        return TaskLinkingResult(
            task_id=plan.task_id,
            task_version=task_version,
            execution_plan_id=plan.id,
            execution_plan_digest=plan.digest,
            linked_file_count=len(hardlink_journal_ids),
            client_fetch_count=client_fetch_count,
            hardlink_journal_ids=tuple(hardlink_journal_ids),
            directory_journal_ids=tuple(dict.fromkeys(directory_journal_ids)),
            replayed=replayed,
        )

    def _load_task_status(self, unit_id: str, plan_id: str) -> TaskStatus:
        with self._session_factory() as session:
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_unit_id != unit_id:
                raise _linking_plan_not_found()
            task = TaskRepository(session).get(plan.task_id)
            if task is None:
                raise _linking_plan_not_found()
            try:
                return TaskStatus(task.status)
            except ValueError as exc:
                raise _linking_plan_invalid("任务包含未知状态") from exc

    def _reserve_linking(
        self,
        unit_id: str,
        current: ExecutionPlanView,
    ) -> _AuthorizedPlan:
        with self._session_factory() as session:
            plan_repository = TaskExecutionPlanRepository(session)
            plan_record = plan_repository.get(current.id)
            latest_plan = plan_repository.latest(unit_id)
            if (
                plan_record is None
                or latest_plan is None
                or latest_plan.id != current.id
                or plan_record.plan_digest != current.plan_digest
                or not plan_record.ready
                or plan_record.blocked_reasons
            ):
                raise _linking_plan_not_current()

            authorized = self._validate_plan_graph(session, plan_record)
            task_repository = TaskRepository(session)
            task = task_repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.AWAITING_CONFIRMATION.value
                or task.version != authorized.task_version_before_linking
            ):
                raise _linking_plan_not_current()

            checkpoint = {
                "schema_version": LINKING_CHECKPOINT_SCHEMA_VERSION,
                "stage": TaskStatus.LINKING.value,
                "execution_plan_id": authorized.id,
                "execution_plan_digest": authorized.digest,
                "execution_gate_id": authorized.gate_id,
                "execution_gate_digest": authorized.gate_digest,
                "task_version_before_linking": authorized.task_version_before_linking,
                "target_downloader_id": authorized.target_downloader_id,
                "target_downloader_version": authorized.target_downloader_version,
                "target_downloader_binding_digest": authorized.target_downloader_binding_digest,
                "target_remote_save_path": authorized.target_remote_save_path,
            }
            try:
                task = task_repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.LINKING,
                    event_type="LINKING_STARTED",
                    reason="current execution plan 已获得文件系统执行授权",
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise _linking_plan_not_current() from exc
            session.commit()
            return replace(authorized, linking_task_version=task.version)

    def _load_reserved_plan(self, unit_id: str, plan_id: str) -> _AuthorizedPlan:
        with self._session_factory() as session:
            plan_record = TaskExecutionPlanRepository(session).get(plan_id)
            if plan_record is None or plan_record.task_unit_id != unit_id:
                raise _linking_plan_not_found()
            task = TaskRepository(session).get(plan_record.task_id)
            if task is None or task.status != TaskStatus.LINKING.value:
                raise _linking_plan_not_current()
            gate_digest = _required_payload_text(plan_record.payload, "execution_gate_digest")
            expected_checkpoint = {
                "schema_version": LINKING_CHECKPOINT_SCHEMA_VERSION,
                "stage": TaskStatus.LINKING.value,
                "execution_plan_id": plan_record.id,
                "execution_plan_digest": plan_record.plan_digest,
                "execution_gate_id": plan_record.execution_gate_id,
                "execution_gate_digest": gate_digest,
                "task_version_before_linking": plan_record.task_version,
                "target_downloader_id": _required_payload_text(
                    plan_record.payload, "target_downloader_id"
                ),
                "target_downloader_version": _required_payload_int(
                    plan_record.payload, "target_downloader_version"
                ),
                "target_downloader_binding_digest": _required_payload_text(
                    plan_record.payload, "target_downloader_binding_digest"
                ),
                "target_remote_save_path": _required_payload_text(
                    plan_record.payload, "target_remote_save_path"
                ),
            }
            if (
                deepcopy(task.checkpoint) != expected_checkpoint
                or task.version != plan_record.task_version + 1
            ):
                raise ApplicationError(
                    code="LINKING_CHECKPOINT_MISMATCH",
                    status=409,
                    title="链接恢复检查点不匹配",
                    detail="LINKING 任务没有与指定 execution plan 匹配的持久化授权检查点",
                )
            gate = TaskExecutionGateRepository(session).get(plan_record.execution_gate_id)
            if gate is None or gate.gate_digest != gate_digest:
                raise _linking_plan_invalid("授权 execution gate 已不可用")
            return _authorized_plan_from_record(
                plan_record,
                linking_task_version=task.version,
            )

    def _advance_to_adding(
        self,
        plan: _AuthorizedPlan,
        *,
        hardlink_journal_ids: tuple[str, ...],
        directory_journal_ids: tuple[str, ...],
        client_fetch_count: int,
    ) -> int:
        checkpoint = {
            "schema_version": ADDING_CHECKPOINT_SCHEMA_VERSION,
            "stage": TaskStatus.ADDING.value,
            "execution_plan_id": plan.id,
            "execution_plan_digest": plan.digest,
            "execution_gate_id": plan.gate_id,
            "execution_gate_digest": plan.gate_digest,
            "task_version_before_linking": plan.task_version_before_linking,
            "target_downloader_id": plan.target_downloader_id,
            "target_downloader_version": plan.target_downloader_version,
            "target_downloader_binding_digest": plan.target_downloader_binding_digest,
            "target_remote_save_path": plan.target_remote_save_path,
            "hardlink_journal_ids": list(hardlink_journal_ids),
            "directory_journal_ids": list(directory_journal_ids),
            "client_fetch_count": client_fetch_count,
        }
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(plan.task_id)
            if (
                task is None
                or task.status != TaskStatus.LINKING.value
                or task.version != plan.linking_task_version
            ):
                raise ApplicationError(
                    code="LINKING_TASK_CHANGED",
                    status=409,
                    title="链接任务状态已经变化",
                    detail="文件动作完成后无法安全提交 ADDING 检查点",
                )
            try:
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.ADDING,
                    event_type="LINKING_COMPLETED",
                    reason=(
                        "文件系统 operation journals 已确认完成："
                        f"{len(hardlink_journal_ids)} 个 hardlink、"
                        f"{len(directory_journal_ids)} 个目录；进入暂停添加阶段"
                    ),
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise ApplicationError(
                    code="LINKING_TASK_CHANGED",
                    status=409,
                    title="链接任务状态已经变化",
                    detail="文件动作完成后无法安全提交 ADDING 检查点",
                ) from exc
            session.commit()
            return task.version

    def _load_completed_linking_result(
        self,
        unit_id: str,
        plan_id: str,
    ) -> TaskLinkingResult:
        with self._session_factory() as session:
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_unit_id != unit_id:
                raise _linking_plan_not_found()
            task = TaskRepository(session).get(plan.task_id)
            if task is None or task.status != TaskStatus.ADDING.value:
                raise _linking_plan_not_current()
            checkpoint = deepcopy(task.checkpoint)
            task_id = task.id
            task_version = task.version
            plan_record_id = plan.id
            plan_record_digest = plan.plan_digest
        if (
            checkpoint.get("schema_version") != ADDING_CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("stage") != TaskStatus.ADDING.value
            or checkpoint.get("execution_plan_id") != plan_record_id
            or checkpoint.get("execution_plan_digest") != plan_record_digest
        ):
            raise ApplicationError(
                code="LINKING_CHECKPOINT_MISMATCH",
                status=409,
                title="链接恢复检查点不匹配",
                detail="ADDING 任务没有与指定 execution plan 匹配的持久化链接结果",
            )
        hardlinks = _required_string_list(checkpoint, "hardlink_journal_ids")
        directories = _required_string_list(checkpoint, "directory_journal_ids")
        client_fetch_count = _required_nonnegative_int(checkpoint, "client_fetch_count")
        return TaskLinkingResult(
            task_id=task_id,
            task_version=task_version,
            execution_plan_id=plan_record_id,
            execution_plan_digest=plan_record_digest,
            linked_file_count=len(hardlinks),
            client_fetch_count=client_fetch_count,
            hardlink_journal_ids=hardlinks,
            directory_journal_ids=directories,
            replayed=True,
        )

    def _validate_plan_graph(
        self,
        session: Session,
        plan_record: TaskExecutionPlanRecord,
    ) -> _AuthorizedPlan:
        authorized = _authorized_plan_from_record(plan_record, linking_task_version=0)
        unit = TaskUnitRepository(session).get(authorized.unit_id)
        task = TaskRepository(session).get(authorized.task_id)
        gate = TaskExecutionGateRepository(session).latest(authorized.unit_id)
        candidate = TaskCandidateRepository(session).get(authorized.candidate_id)
        preflight = PreflightSnapshotRepository(session).latest_for_task(authorized.task_id)
        review = TaskReviewRepository(session).latest(
            task_unit_id=authorized.unit_id,
            preflight_snapshot_id=authorized.preflight_snapshot_id,
        )
        gate_review_version = gate.payload.get("review_version") if gate is not None else None
        if (
            unit is None
            or task is None
            or gate is None
            or gate.id != authorized.gate_id
            or gate.gate_digest != authorized.gate_digest
            or not gate.eligible
            or gate.task_version != authorized.task_version_before_linking
            or gate.candidate_id != authorized.candidate_id
            or gate.preflight_snapshot_id != authorized.preflight_snapshot_id
            or gate.review_revision_id != authorized.review_revision_id
            or candidate is None
            or candidate.preflight_snapshot_id != authorized.preflight_snapshot_id
            or candidate.metainfo_digest != gate.metainfo_digest
            or preflight is None
            or preflight.id != authorized.preflight_snapshot_id
            or review is None
            or review.id != authorized.review_revision_id
            or review.approved_candidate_id != authorized.candidate_id
            or not isinstance(gate_review_version, int)
            or review.version != gate_review_version
            or unit.source_root != authorized.source_root
            or unit.source_inventory_digest != authorized.source_inventory_digest
        ):
            raise _linking_plan_not_current()
        return authorized

    def _assert_source_inventory_current(self, plan: _AuthorizedPlan) -> None:
        normalized_root = SafeFilesystemGateway(self._data_root).normalize_relative_path(
            plan.source_root,
            allow_root=True,
        )
        source_root = (
            self._data_root
            if normalized_root == "."
            else self._data_root.joinpath(*normalized_root.split("/"))
        )
        try:
            observed = source_inventory_digest(scan_source_inventory(source_root))
        except DomainViolation as exc:
            raise ApplicationError(
                code="LINKING_SOURCE_UNAVAILABLE",
                status=409,
                title="链接源目录不可用",
                detail="执行文件系统副作用前无法重新确认 source inventory",
            ) from exc
        if observed != plan.source_inventory_digest:
            raise ApplicationError(
                code="LINKING_SOURCE_CHANGED",
                status=409,
                title="链接源目录已变化",
                detail="execution plan 授权后 source inventory 已变化，尚未执行新的文件副作用",
            )


def _authorized_plan_from_record(
    record: TaskExecutionPlanRecord,
    *,
    linking_task_version: int,
) -> _AuthorizedPlan:
    payload = record.payload
    try:
        actions = execution_plan_actions_from_payload(payload)
    except ValueError as exc:
        raise _linking_plan_invalid(str(exc)) from exc
    if not record.ready or record.blocked_reasons:
        raise _linking_plan_not_current()
    return _AuthorizedPlan(
        id=record.id,
        digest=record.plan_digest,
        task_id=record.task_id,
        task_version_before_linking=record.task_version,
        linking_task_version=linking_task_version,
        unit_id=record.task_unit_id,
        gate_id=record.execution_gate_id,
        gate_digest=_required_payload_text(payload, "execution_gate_digest"),
        candidate_id=record.candidate_id,
        preflight_snapshot_id=_required_payload_text(payload, "preflight_snapshot_id"),
        review_revision_id=_required_payload_text(payload, "review_revision_id"),
        source_root=_required_payload_text(payload, "source_root"),
        source_inventory_digest=_required_payload_text(payload, "source_inventory_digest"),
        target_root=record.target_root,
        target_downloader_id=_required_payload_text(payload, "target_downloader_id"),
        target_downloader_version=_required_payload_int(payload, "target_downloader_version"),
        target_downloader_binding_digest=_required_payload_text(
            payload, "target_downloader_binding_digest"
        ),
        target_remote_save_path=_required_payload_text(payload, "target_remote_save_path"),
        actions=actions,
        client_check_required=record.client_check_required,
    )


def _join_relative_root(root: str, relative: str) -> str:
    return relative if root == "." else f"{root}/{relative}"


def _required_payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _linking_plan_invalid(f"execution plan 缺少 {key}")
    return value


def _required_payload_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _linking_plan_invalid(f"execution plan 缺少有效 {key}")
    return value


def _required_string_list(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _linking_plan_invalid(f"checkpoint 缺少有效 {key}")
    return tuple(value)


def _required_nonnegative_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _linking_plan_invalid(f"checkpoint 缺少有效 {key}")
    return value


def _linking_plan_not_found() -> ApplicationError:
    return ApplicationError(
        code="LINKING_PLAN_NOT_FOUND",
        status=404,
        title="执行计划不存在",
        detail="指定 execution plan 不存在或不属于当前 task unit",
    )


def _linking_plan_not_current() -> ApplicationError:
    return ApplicationError(
        code="LINKING_PLAN_NOT_CURRENT",
        status=409,
        title="执行计划已经失效",
        detail="只有 latest、current、ready 且仍绑定当前 gate/review/task 的计划才能进入 LINKING",
    )


def _linking_plan_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="LINKING_PLAN_INVALID",
        status=409,
        title="执行计划证据无效",
        detail=detail,
    )
