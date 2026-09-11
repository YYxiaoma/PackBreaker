from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QbittorrentRemoveOperationRequest,
    QbittorrentRemoveOperationService,
)
from backend.app.application.downloaders import QbittorrentWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_DIRECTORY_OPERATION,
    CREATE_HARDLINK_OPERATION,
    FilesystemOperationService,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.models import TaskExecutionPlanRecord
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)

CANCELLATION_CHECKPOINT_SCHEMA_VERSION = "packbreaker-cancellation-checkpoint-v1"

_SIDE_EFFECT_STATUSES = frozenset(
    {
        TaskStatus.LINKING,
        TaskStatus.ADDING,
        TaskStatus.CLIENT_VERIFYING,
        TaskStatus.SEEDING,
        TaskStatus.CANCELLING,
        TaskStatus.ROLLING_BACK,
    }
)


class QbittorrentBindingProvider(Protocol):
    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBinding: ...


@dataclass(frozen=True, slots=True)
class TaskCancellationRequest:
    task_id: str
    remove_downloader_task: bool
    rollback_created_resources: bool


@dataclass(frozen=True, slots=True)
class TaskCancellationResult:
    task_id: str
    task_version: int
    status: TaskStatus
    execution_plan_id: str
    qbit_remove_journal_id: str | None
    rolled_back_hardlink_journal_ids: tuple[str, ...]
    rolled_back_directory_journal_ids: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _RollbackPlan:
    task_id: str
    task_version: int
    execution_plan_id: str
    execution_plan_digest: str
    target_downloader_id: str
    target_downloader_version: int
    target_downloader_binding_digest: str
    remove_downloader_task: bool
    rollback_created_resources: bool
    qbit_add_journal_id: str | None
    torrent_hash: str | None
    remote_save_path: str | None
    ownership_tag: str | None
    hardlink_journal_ids: tuple[str, ...]
    directory_journal_ids: tuple[str, ...]


class TaskCancellationCoordinator:
    """按显式选项取消已进入副作用阶段的任务；只回收 journal-owned 资源。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: QbittorrentBindingProvider,
        qbit_remove_operations: QbittorrentRemoveOperationService,
        filesystem_operations: FilesystemOperationService,
    ) -> None:
        self._session_factory = session_factory
        self._downloader_service = downloader_service
        self._qbit_remove_operations = qbit_remove_operations
        self._filesystem_operations = filesystem_operations

    async def execute(
        self,
        request: TaskCancellationRequest,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskCancellationResult:
        plan, replayed = self._reserve_or_load(request)
        qbit_remove_journal_id: str | None = None

        if (
            plan.qbit_add_journal_id is not None
            and plan.rollback_created_resources
            and not plan.remove_downloader_task
        ):
            raise ApplicationError(
                code="CANCELLATION_DOWNLOADER_REQUIRED",
                status=409,
                title="回滚文件前必须先移除下载器任务",
                detail="qBittorrent 仍持有目标路径时禁止删除 PackBreaker 创建的 hardlink",
            )

        if plan.remove_downloader_task and plan.qbit_add_journal_id is not None:
            binding = self._load_binding(plan)
            remove_result = await self._qbit_remove_operations.execute(
                QbittorrentRemoveOperationRequest(
                    task_id=plan.task_id,
                    candidate_key=_remove_candidate_key(plan),
                    downloader_id=plan.target_downloader_id,
                    downloader_version=plan.target_downloader_version,
                    execution_plan_id=plan.execution_plan_id,
                    qbit_add_journal_id=plan.qbit_add_journal_id,
                    torrent_hash=_required_optional(plan.torrent_hash, "torrent_hash"),
                    remote_save_path=_required_optional(plan.remote_save_path, "remote_save_path"),
                    ownership_tag=_required_optional(plan.ownership_tag, "ownership_tag"),
                ),
                binding,
            )
            qbit_remove_journal_id = remove_result.journal_id
            if fault_hook is not None:
                fault_hook("after_qb_removed")

        rolled_back_hardlinks: list[str] = []
        rolled_back_directories: list[str] = []
        if plan.rollback_created_resources:
            for journal_id in reversed(plan.hardlink_journal_ids):
                result = self._filesystem_operations.rollback_journal(journal_id)
                if result.status is OperationStatus.ROLLED_BACK:
                    rolled_back_hardlinks.append(journal_id)
                if fault_hook is not None:
                    fault_hook(f"after_hardlink_rollback:{journal_id}")
            for journal_id in reversed(plan.directory_journal_ids):
                result = self._filesystem_operations.rollback_journal(journal_id)
                if result.status is OperationStatus.ROLLED_BACK:
                    rolled_back_directories.append(journal_id)
                if fault_hook is not None:
                    fault_hook(f"after_directory_rollback:{journal_id}")

        return self._complete(
            plan,
            qbit_remove_journal_id=qbit_remove_journal_id,
            rolled_back_hardlink_journal_ids=tuple(rolled_back_hardlinks),
            rolled_back_directory_journal_ids=tuple(rolled_back_directories),
            replayed=replayed,
        )

    async def resume(self, task_id: str) -> TaskCancellationResult:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _cancellation_not_found()
            checkpoint = deepcopy(task.checkpoint)
            if checkpoint.get(
                "schema_version"
            ) != CANCELLATION_CHECKPOINT_SCHEMA_VERSION or task.status not in {
                TaskStatus.ROLLING_BACK.value,
                TaskStatus.CANCELLED.value,
            }:
                raise _cancellation_state_invalid("任务没有可恢复的取消/回滚检查点")
            request = TaskCancellationRequest(
                task_id=task.id,
                remove_downloader_task=_required_bool(checkpoint, "remove_downloader_task"),
                rollback_created_resources=_required_bool(
                    checkpoint,
                    "rollback_created_resources",
                ),
            )
        return await self.execute(request)

    def _reserve_or_load(
        self,
        request: TaskCancellationRequest,
    ) -> tuple[_RollbackPlan, bool]:
        with self._session_factory() as session:
            task_repository = TaskRepository(session)
            task = task_repository.get(request.task_id)
            if task is None:
                raise _cancellation_not_found()
            try:
                status = TaskStatus(task.status)
            except ValueError as exc:
                raise _cancellation_state_invalid("任务包含未知状态") from exc
            if status is TaskStatus.CANCELLED:
                return self._load_cancelled(session, request), True
            if status not in _SIDE_EFFECT_STATUSES:
                raise _cancellation_state_invalid(
                    "当前切片只处理已进入 LINKING 及之后副作用阶段的取消"
                )
            if status is TaskStatus.ROLLING_BACK:
                return self._load_reserved(session, request), True

            checkpoint = deepcopy(task.checkpoint)
            plan_id = _required_text(checkpoint, "execution_plan_id")
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_id != task.id:
                raise _cancellation_evidence_invalid("取消检查点引用的 execution plan 无效")
            if checkpoint.get("execution_plan_digest") != plan.plan_digest:
                raise _cancellation_evidence_invalid("取消检查点与 execution plan digest 不一致")

            rollback = self._build_rollback_plan(
                session,
                task_id=task.id,
                task_version=task.version,
                plan=plan,
                remove_downloader_task=request.remove_downloader_task,
                rollback_created_resources=request.rollback_created_resources,
            )
            rollback_checkpoint = _rollback_checkpoint(rollback, stage=TaskStatus.ROLLING_BACK)
            try:
                if status is TaskStatus.SEEDING:
                    task = task_repository.transition(
                        task_id=task.id,
                        expected_version=task.version,
                        to_status=TaskStatus.CANCELLING,
                        event_type="CANCELLATION_STARTED",
                        reason="用户请求取消已进入做种准备的任务",
                        checkpoint=rollback_checkpoint,
                    )
                    session.flush()
                    status = TaskStatus.CANCELLING
                task = task_repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.ROLLING_BACK,
                    event_type="ROLLBACK_STARTED",
                    reason="取消请求已冻结，开始按 operation journal 回收资源",
                    checkpoint=rollback_checkpoint,
                )
            except DomainViolation as exc:
                raise _cancellation_task_changed() from exc
            session.commit()
            return _with_task_version(rollback, task.version), False

    def _load_reserved(
        self,
        session: Session,
        request: TaskCancellationRequest,
    ) -> _RollbackPlan:
        task = TaskRepository(session).get(request.task_id)
        if task is None or task.status != TaskStatus.ROLLING_BACK.value:
            raise _cancellation_state_invalid("任务不处于 ROLLING_BACK")
        checkpoint = deepcopy(task.checkpoint)
        if (
            checkpoint.get("schema_version") != CANCELLATION_CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("stage") != TaskStatus.ROLLING_BACK.value
            or checkpoint.get("remove_downloader_task") != request.remove_downloader_task
            or checkpoint.get("rollback_created_resources") != request.rollback_created_resources
        ):
            raise ApplicationError(
                code="CANCELLATION_OPTION_CONFLICT",
                status=409,
                title="取消重放选项与已冻结请求不一致",
                detail="ROLLING_BACK 必须使用首次取消时冻结的 remove/rollback 选项",
            )
        return _rollback_plan_from_checkpoint(task.id, task.version, checkpoint)

    def _load_cancelled(
        self,
        session: Session,
        request: TaskCancellationRequest,
    ) -> _RollbackPlan:
        task = TaskRepository(session).get(request.task_id)
        if task is None:
            raise _cancellation_not_found()
        checkpoint = deepcopy(task.checkpoint)
        if (
            checkpoint.get("schema_version") != CANCELLATION_CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("stage") != TaskStatus.CANCELLED.value
        ):
            raise _cancellation_evidence_invalid("CANCELLED 任务缺少 PackBreaker 取消检查点")
        if (
            checkpoint.get("remove_downloader_task") != request.remove_downloader_task
            or checkpoint.get("rollback_created_resources") != request.rollback_created_resources
        ):
            raise ApplicationError(
                code="CANCELLATION_OPTION_CONFLICT",
                status=409,
                title="取消重放选项与已完成请求不一致",
                detail="CANCELLED 任务只能按首次取消时冻结的 remove/rollback 选项重放",
            )
        return _rollback_plan_from_checkpoint(task.id, task.version, checkpoint)

    def _build_rollback_plan(
        self,
        session: Session,
        *,
        task_id: str,
        task_version: int,
        plan: TaskExecutionPlanRecord,
        remove_downloader_task: bool,
        rollback_created_resources: bool,
    ) -> _RollbackPlan:
        journal_repository = OperationJournalRepository(session)
        file_journals = journal_repository.list_for_task(
            task_id,
            operation_types=(CREATE_HARDLINK_OPERATION, CREATE_DIRECTORY_OPERATION),
        )
        unresolved_files = tuple(
            journal
            for journal in file_journals
            if OperationStatus(journal.status)
            not in {
                OperationStatus.APPLIED,
                OperationStatus.NOOP,
                OperationStatus.ROLLBACK_PENDING,
                OperationStatus.ROLLED_BACK,
            }
        )
        if unresolved_files:
            raise _cancellation_evidence_invalid(
                "存在所有权或回滚结果尚未明确的文件系统 journal，必须先完成对账"
            )
        hardlinks = tuple(
            journal.id
            for journal in file_journals
            if journal.operation_type == CREATE_HARDLINK_OPERATION
            and OperationStatus(journal.status)
            in {
                OperationStatus.APPLIED,
                OperationStatus.ROLLBACK_PENDING,
                OperationStatus.ROLLED_BACK,
            }
        )
        directories = tuple(
            journal.id
            for journal in file_journals
            if journal.operation_type == CREATE_DIRECTORY_OPERATION
            and OperationStatus(journal.status)
            in {
                OperationStatus.APPLIED,
                OperationStatus.ROLLBACK_PENDING,
                OperationStatus.ROLLED_BACK,
            }
        )

        add_journals = tuple(
            journal
            for journal in journal_repository.list_for_task(
                task_id,
                operation_types=(QBITTORRENT_ADD_OPERATION,),
            )
            if journal.intent.get("execution_plan_id") == plan.id
        )
        if len(add_journals) > 1:
            raise _cancellation_evidence_invalid("同一 execution plan 出现多个 qB add journal")
        add_journal = add_journals[0] if add_journals else None
        qbit_add_journal_id: str | None = None
        torrent_hash: str | None = None
        remote_save_path: str | None = None
        ownership_tag: str | None = None
        if add_journal is not None:
            if OperationStatus(add_journal.status) is not OperationStatus.APPLIED:
                raise _cancellation_evidence_invalid(
                    "qB add journal 尚未 APPLIED，必须先完成添加结果对账再自动取消"
                )
            if add_journal.after_snapshot is None:
                raise _cancellation_evidence_invalid("qB add journal 缺少 after snapshot")
            qbit_add_journal_id = add_journal.id
            torrent_hash = _required_text(add_journal.after_snapshot, "torrent_hash")
            remote_save_path = _required_text(add_journal.after_snapshot, "save_path")
            ownership_tag = _required_text(add_journal.after_snapshot, "ownership_tag")
            if rollback_created_resources and not remove_downloader_task:
                raise ApplicationError(
                    code="CANCELLATION_DOWNLOADER_REQUIRED",
                    status=409,
                    title="回滚文件前必须先移除下载器任务",
                    detail="qBittorrent 仍持有目标路径时禁止删除 PackBreaker 创建的 hardlink",
                )

        payload = plan.payload
        return _RollbackPlan(
            task_id=task_id,
            task_version=task_version,
            execution_plan_id=plan.id,
            execution_plan_digest=plan.plan_digest,
            target_downloader_id=_required_text(payload, "target_downloader_id"),
            target_downloader_version=_required_positive_int(payload, "target_downloader_version"),
            target_downloader_binding_digest=_required_text(
                payload,
                "target_downloader_binding_digest",
            ),
            remove_downloader_task=remove_downloader_task,
            rollback_created_resources=rollback_created_resources,
            qbit_add_journal_id=qbit_add_journal_id,
            torrent_hash=torrent_hash,
            remote_save_path=remote_save_path,
            ownership_tag=ownership_tag,
            hardlink_journal_ids=hardlinks,
            directory_journal_ids=directories,
        )

    def _load_binding(self, plan: _RollbackPlan) -> QbittorrentWriteBinding:
        try:
            binding = self._downloader_service.qbittorrent_write_binding(plan.target_downloader_id)
        except ApplicationError as exc:
            raise ApplicationError(
                code="CANCELLATION_DOWNLOADER_CHANGED",
                status=409,
                title="取消时目标 qBittorrent 配置不可用",
                detail="无法证明当前写 binding 仍指向 execution plan 冻结的下载器",
            ) from exc
        if (
            binding.downloader_version != plan.target_downloader_version
            or binding.binding_digest != plan.target_downloader_binding_digest
        ):
            raise ApplicationError(
                code="CANCELLATION_DOWNLOADER_CHANGED",
                status=409,
                title="取消时目标 qBittorrent 配置已变化",
                detail="禁止使用不同 version/binding 的下载器移除既有任务",
            )
        return binding

    def _complete(
        self,
        plan: _RollbackPlan,
        *,
        qbit_remove_journal_id: str | None,
        rolled_back_hardlink_journal_ids: tuple[str, ...],
        rolled_back_directory_journal_ids: tuple[str, ...],
        replayed: bool,
    ) -> TaskCancellationResult:
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(plan.task_id)
            if task is None:
                raise _cancellation_not_found()
            if task.status == TaskStatus.CANCELLED.value:
                checkpoint = deepcopy(task.checkpoint)
                return _result_from_checkpoint(task.id, task.version, checkpoint, replayed=True)
            if task.status != TaskStatus.ROLLING_BACK.value:
                raise _cancellation_task_changed()
            checkpoint = _rollback_checkpoint(plan, stage=TaskStatus.CANCELLED)
            checkpoint.update(
                {
                    "qbit_remove_journal_id": qbit_remove_journal_id,
                    "rolled_back_hardlink_journal_ids": list(rolled_back_hardlink_journal_ids),
                    "rolled_back_directory_journal_ids": list(rolled_back_directory_journal_ids),
                }
            )
            try:
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.CANCELLED,
                    event_type="ROLLBACK_COMPLETED",
                    reason="用户请求的下载器移除与 journal-owned 文件回滚已完成",
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise _cancellation_task_changed() from exc
            session.commit()
            return TaskCancellationResult(
                task_id=task.id,
                task_version=task.version,
                status=TaskStatus.CANCELLED,
                execution_plan_id=plan.execution_plan_id,
                qbit_remove_journal_id=qbit_remove_journal_id,
                rolled_back_hardlink_journal_ids=rolled_back_hardlink_journal_ids,
                rolled_back_directory_journal_ids=rolled_back_directory_journal_ids,
                replayed=replayed,
            )


def _rollback_checkpoint(plan: _RollbackPlan, *, stage: TaskStatus) -> dict[str, object]:
    return {
        "schema_version": CANCELLATION_CHECKPOINT_SCHEMA_VERSION,
        "stage": stage.value,
        "execution_plan_id": plan.execution_plan_id,
        "execution_plan_digest": plan.execution_plan_digest,
        "target_downloader_id": plan.target_downloader_id,
        "target_downloader_version": plan.target_downloader_version,
        "target_downloader_binding_digest": plan.target_downloader_binding_digest,
        "remove_downloader_task": plan.remove_downloader_task,
        "rollback_created_resources": plan.rollback_created_resources,
        "qbit_add_journal_id": plan.qbit_add_journal_id,
        "torrent_hash": plan.torrent_hash,
        "remote_save_path": plan.remote_save_path,
        "ownership_tag": plan.ownership_tag,
        "hardlink_journal_ids": list(plan.hardlink_journal_ids),
        "directory_journal_ids": list(plan.directory_journal_ids),
    }


def _rollback_plan_from_checkpoint(
    task_id: str,
    task_version: int,
    checkpoint: dict[str, object],
) -> _RollbackPlan:
    return _RollbackPlan(
        task_id=task_id,
        task_version=task_version,
        execution_plan_id=_required_text(checkpoint, "execution_plan_id"),
        execution_plan_digest=_required_text(checkpoint, "execution_plan_digest"),
        target_downloader_id=_required_text(checkpoint, "target_downloader_id"),
        target_downloader_version=_required_positive_int(checkpoint, "target_downloader_version"),
        target_downloader_binding_digest=_required_text(
            checkpoint,
            "target_downloader_binding_digest",
        ),
        remove_downloader_task=_required_bool(checkpoint, "remove_downloader_task"),
        rollback_created_resources=_required_bool(checkpoint, "rollback_created_resources"),
        qbit_add_journal_id=_optional_text(checkpoint, "qbit_add_journal_id"),
        torrent_hash=_optional_text(checkpoint, "torrent_hash"),
        remote_save_path=_optional_text(checkpoint, "remote_save_path"),
        ownership_tag=_optional_text(checkpoint, "ownership_tag"),
        hardlink_journal_ids=_required_string_tuple(checkpoint, "hardlink_journal_ids"),
        directory_journal_ids=_required_string_tuple(checkpoint, "directory_journal_ids"),
    )


def _result_from_checkpoint(
    task_id: str,
    task_version: int,
    checkpoint: dict[str, object],
    *,
    replayed: bool,
) -> TaskCancellationResult:
    if (
        checkpoint.get("schema_version") != CANCELLATION_CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("stage") != TaskStatus.CANCELLED.value
    ):
        raise _cancellation_evidence_invalid("CANCELLED checkpoint 格式无效")
    return TaskCancellationResult(
        task_id=task_id,
        task_version=task_version,
        status=TaskStatus.CANCELLED,
        execution_plan_id=_required_text(checkpoint, "execution_plan_id"),
        qbit_remove_journal_id=_optional_text(checkpoint, "qbit_remove_journal_id"),
        rolled_back_hardlink_journal_ids=_required_string_tuple(
            checkpoint,
            "rolled_back_hardlink_journal_ids",
        ),
        rolled_back_directory_journal_ids=_required_string_tuple(
            checkpoint,
            "rolled_back_directory_journal_ids",
        ),
        replayed=replayed,
    )


def _remove_candidate_key(plan: _RollbackPlan) -> str:
    value = f"{plan.execution_plan_id}:{plan.qbit_add_journal_id}:remove"
    return sha256(value.encode("utf-8")).hexdigest()


def _with_task_version(plan: _RollbackPlan, task_version: int) -> _RollbackPlan:
    return _RollbackPlan(
        task_id=plan.task_id,
        task_version=task_version,
        execution_plan_id=plan.execution_plan_id,
        execution_plan_digest=plan.execution_plan_digest,
        target_downloader_id=plan.target_downloader_id,
        target_downloader_version=plan.target_downloader_version,
        target_downloader_binding_digest=plan.target_downloader_binding_digest,
        remove_downloader_task=plan.remove_downloader_task,
        rollback_created_resources=plan.rollback_created_resources,
        qbit_add_journal_id=plan.qbit_add_journal_id,
        torrent_hash=plan.torrent_hash,
        remote_save_path=plan.remote_save_path,
        ownership_tag=plan.ownership_tag,
        hardlink_journal_ids=plan.hardlink_journal_ids,
        directory_journal_ids=plan.directory_journal_ids,
    )


def _required_optional(value: str | None, key: str) -> str:
    if value is None:
        raise _cancellation_evidence_invalid(f"取消证据缺少 {key}")
    return value


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _cancellation_evidence_invalid(f"取消证据缺少有效 {key}")
    return value


def _optional_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _cancellation_evidence_invalid(f"取消证据包含无效 {key}")
    return value


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _cancellation_evidence_invalid(f"取消证据缺少有效 {key}")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise _cancellation_evidence_invalid(f"取消证据缺少有效 {key}")
    return value


def _required_string_tuple(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _cancellation_evidence_invalid(f"取消证据缺少有效 {key}")
    return tuple(value)


def _cancellation_not_found() -> ApplicationError:
    return ApplicationError(
        code="CANCELLATION_TASK_NOT_FOUND",
        status=404,
        title="取消任务不存在",
        detail="指定任务不存在",
    )


def _cancellation_state_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="CANCELLATION_TASK_STATE_INVALID",
        status=409,
        title="任务状态不允许执行当前取消链",
        detail=detail,
    )


def _cancellation_evidence_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="CANCELLATION_EVIDENCE_INVALID",
        status=409,
        title="取消/回滚证据无效",
        detail=detail,
    )


def _cancellation_task_changed() -> ApplicationError:
    return ApplicationError(
        code="CANCELLATION_TASK_CHANGED",
        status=409,
        title="取消任务状态已经变化",
        detail="取消/回滚执行期间 task version 或状态发生变化，请重新读取后对账",
    )
