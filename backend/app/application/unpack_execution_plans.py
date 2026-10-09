from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_existing_reuse import verify_existing_reuse
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import ExecutionPlanBlockReason
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import FileSnapshot, VerificationLevel
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    utc_now,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import current_file_snapshot
from backend.app.infrastructure.torrent_parser import parse_torrent


class UnpackExecutionPlanSiteProvider(Protocol):
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...


class UnpackExecutionPlanDownloaderProvider(Protocol):
    def write_binding(
        self,
        downloader_id: str,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class UnpackExecutionPlanningReport:
    execution_id: str
    processed_count: int
    ready_count: int
    blocked_count: int
    error_count: int
    execution_status: UnpackExecutionStatus


@dataclass(frozen=True, slots=True)
class _PlanContext:
    execution_id: str
    item_id: str
    item_version: int
    candidate_id: str
    generation: int
    metainfo_digest: str
    site_config_id: str
    site_config_version: int
    adapter_site_id: str
    torrent_id: str
    output_directory: str
    storage_mode: TaskStorageMode
    conflict_policy: TaskConflictPolicy
    target_downloader_id: str
    mapping_evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _TargetLayout:
    device: int
    blocked_reasons: tuple[ExecutionPlanBlockReason, ...]
    create_directories: tuple[str, ...]


class UnpackExecutionPlanService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackExecutionPlanSiteProvider,
        downloader_provider: UnpackExecutionPlanDownloaderProvider,
        *,
        path_scope: AuthorizedPathScope,
        fetch_timeout_seconds: float = 30.0,
    ) -> None:
        if fetch_timeout_seconds <= 0:
            raise ValueError("执行计划 torrent 获取超时必须大于 0")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._downloader_provider = downloader_provider
        self._path_scope = path_scope
        self._filesystem = SafeFilesystemGateway(path_scope.legacy_data_root, path_scope=path_scope)
        self._fetch_timeout_seconds = fetch_timeout_seconds

    def list_plannable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("执行计划 execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackExecution.id)
                    .where(
                        select(UnpackExecutionItem.id)
                        .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
                        .where(
                            UnpackExecutionItem.status == UnpackItemStatus.CONTENT_VERIFIED.value
                        )
                        .exists()
                    )
                    .order_by(UnpackExecution.updated_at, UnpackExecution.id)
                    .limit(limit)
                ).all()
            )

    async def plan_next_batch(
        self,
        execution_id: str,
        *,
        limit: int = 5,
    ) -> UnpackExecutionPlanningReport:
        if limit < 1 or limit > 100:
            raise self._invalid("执行计划批次必须位于 1 到 100 之间")
        item_ids = self._list_item_ids(execution_id, limit=limit)
        processed = 0
        for item_id in item_ids:
            try:
                context = self._load_context(item_id)
                plan = await self._build_plan(context)
                self._persist_plan(context, plan)
            except (ApplicationError, SiteAdapterError, DomainViolation, ValueError) as exc:
                code, message = _safe_error(exc)
                self._mark_error(item_id, code=code, message=message)
            processed += 1
        return self._report(execution_id, processed_count=processed)

    def _list_item_ids(self, execution_id: str, *, limit: int) -> tuple[str, ...]:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            return tuple(
                session.scalars(
                    select(UnpackExecutionItem.id)
                    .where(UnpackExecutionItem.execution_id == execution_id)
                    .where(UnpackExecutionItem.status == UnpackItemStatus.CONTENT_VERIFIED.value)
                    .order_by(UnpackExecutionItem.id)
                    .limit(limit)
                ).all()
            )

    def _load_context(self, item_id: str) -> _PlanContext:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.status != UnpackItemStatus.CONTENT_VERIFIED.value:
                raise self._conflict("影片项当前不处于可生成执行计划状态")
            if item.content_verification_level != VerificationLevel.FULL_VERIFIED.value:
                raise self._conflict("只有 FULL_VERIFIED 影片项可以生成执行计划")
            if item.execution_plan is not None or item.execution_plan_digest is not None:
                raise self._conflict("影片项已经存在冻结执行计划")
            if item.selected_candidate_id is None or item.torrent_metainfo_digest is None:
                raise self._conflict("影片项缺少选中候选或 metainfo digest")

            candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
            if (
                candidate is None
                or candidate.item_id != item.id
                or candidate.generation != item.candidate_generation
                or candidate.verification_status != UnpackCandidateVerificationStatus.VERIFIED.value
                or candidate.verification_level != VerificationLevel.FULL_VERIFIED.value
                or candidate.metainfo_digest != item.torrent_metainfo_digest
            ):
                raise self._conflict("候选验证证据与影片项不一致")

            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._not_found()
            output = execution.config_snapshot.get("output_config")
            if not isinstance(output, dict):
                raise self._conflict("execution 缺少输出配置快照")
            output_directory = output.get("output_directory")
            storage_mode = output.get("storage_mode")
            conflict_policy = output.get("conflict_policy")
            target_downloader_id = output.get("target_downloader_id")
            if (
                not isinstance(output_directory, str)
                or not output_directory
                or not isinstance(storage_mode, str)
                or not isinstance(conflict_policy, str)
                or not isinstance(target_downloader_id, str)
                or not target_downloader_id
            ):
                raise self._conflict("execution 输出配置快照无效")
            try:
                storage = TaskStorageMode(storage_mode)
                conflict = TaskConflictPolicy(conflict_policy)
            except ValueError as exc:
                raise self._conflict("execution 输出策略已无法识别") from exc
            if conflict is not TaskConflictPolicy.VERIFY_REUSE_OR_STOP:
                raise ApplicationError(
                    code="UNPACK_PLAN_CONFLICT_POLICY_UNSUPPORTED",
                    status=409,
                    title="文件冲突策略尚未接入安全执行器",
                    detail="当前 v2 执行只允许 VERIFY_REUSE_OR_STOP",
                )

            raw_ref = dict(candidate.raw_ref)
            version = raw_ref.get("site_config_version")
            adapter_site_id = raw_ref.get("adapter_site_id")
            torrent_id = raw_ref.get("torrent_id")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
                or not isinstance(adapter_site_id, str)
                or not adapter_site_id
                or not isinstance(torrent_id, str)
                or not torrent_id
            ):
                raise self._conflict("候选缺少安全站点引用")

            evidence = dict(candidate.evidence)
            verification = evidence.get("auxiliary_verification")
            if not isinstance(verification, dict):
                verification = evidence.get("content_verification")
            if not isinstance(verification, dict):
                raise self._conflict("候选缺少内容验证映射证据")
            raw_mappings = verification.get("mappings")
            if not isinstance(raw_mappings, list) or any(
                not isinstance(entry, dict) for entry in raw_mappings
            ):
                raise self._conflict("候选内容验证映射证据不完整")
            return _PlanContext(
                execution_id=execution.id,
                item_id=item.id,
                item_version=item.version,
                candidate_id=candidate.id,
                generation=item.candidate_generation,
                metainfo_digest=item.torrent_metainfo_digest,
                site_config_id=candidate.site_id,
                site_config_version=version,
                adapter_site_id=adapter_site_id,
                torrent_id=torrent_id,
                output_directory=output_directory,
                storage_mode=storage,
                conflict_policy=conflict,
                target_downloader_id=target_downloader_id,
                mapping_evidence=tuple(dict(entry) for entry in raw_mappings),
            )

    async def _build_plan(self, context: _PlanContext) -> UnpackExecutionPlan:
        site = next(
            (
                item
                for item in self._site_provider.enabled_adapters()
                if item.config_id == context.site_config_id
            ),
            None,
        )
        if site is None:
            raise self._conflict("候选站点当前不可用")
        if (
            site.config_version != context.site_config_version
            or site.site_id != context.adapter_site_id
        ):
            raise self._conflict("候选站点配置已变化，请重新匹配")
        async with asyncio.timeout(self._fetch_timeout_seconds):
            payload = await site.adapter.fetch_torrent(context.torrent_id)
        if payload.site_id != context.adapter_site_id or payload.torrent_id != context.torrent_id:
            raise self._conflict("重新获取的 torrent 身份与候选不一致")
        meta = parse_torrent(payload.content)
        if meta.metainfo_digest != context.metainfo_digest:
            raise ApplicationError(
                code="UNPACK_PLAN_TORRENT_CHANGED",
                status=409,
                title="候选 torrent 已变化",
                detail="生成执行计划时 metainfo digest 与内容验证证据不一致",
            )

        actions, mapping_blockers = self._build_actions(meta.files, context.mapping_evidence)
        output_path = Path(self._path_scope.normalize_reference(context.output_directory))
        if (
            context.storage_mode is TaskStorageMode.HARDLINK
            and context.conflict_policy is TaskConflictPolicy.VERIFY_REUSE_OR_STOP
        ):
            actions = self._freeze_existing_reuse(output_path, actions)
        layout = self._inspect_target_layout(
            output_path,
            actions,
            storage_mode=context.storage_mode,
        )
        binding = self._downloader_provider.write_binding(context.target_downloader_id)
        try:
            remote_save_path = binding.remote_save_path(output_path)
        except (DomainViolation, ValueError) as exc:
            raise ApplicationError(
                code="UNPACK_PLAN_TARGET_PATH_UNMAPPED",
                status=409,
                title="目标目录无法映射到下载器",
                detail="请检查目标下载器路径映射是否覆盖输出目录",
            ) from exc

        client_check_required = (
            not isinstance(binding, QbittorrentWriteBinding)
            or binding.capabilities.get("supports_skip_checking") is not True
            or context.storage_mode is not TaskStorageMode.HARDLINK
        )
        blockers = tuple(
            sorted(
                {*mapping_blockers, *layout.blocked_reasons},
                key=lambda item: item.value,
            )
        )
        return UnpackExecutionPlan(
            item_id=context.item_id,
            item_version_before=context.item_version,
            candidate_id=context.candidate_id,
            candidate_generation=context.generation,
            metainfo_digest=context.metainfo_digest,
            verification_level=VerificationLevel.FULL_VERIFIED,
            output_directory=output_path.as_posix(),
            storage_mode=context.storage_mode,
            conflict_policy=context.conflict_policy,
            target_device=layout.device,
            target_downloader_id=context.target_downloader_id,
            target_downloader_version=binding.downloader_version,
            target_downloader_binding_digest=binding.binding_digest,
            target_remote_save_path=remote_save_path,
            client_check_required=client_check_required,
            actions=actions,
            create_directories=layout.create_directories,
            blocked_reasons=blockers,
            created_at=utc_now(),
        )

    def _freeze_existing_reuse(
        self,
        output_root: Path,
        actions: tuple[UnpackExecutionAction, ...],
    ) -> tuple[UnpackExecutionAction, ...]:
        frozen: list[UnpackExecutionAction] = []
        for action in actions:
            if action.kind is not UnpackExecutionActionKind.MATERIALIZE:
                frozen.append(action)
                continue
            assert action.source_path is not None and action.source_snapshot is not None
            target = output_root.joinpath(*PurePosixPath(action.torrent_path).parts)
            try:
                target.lstat()
            except FileNotFoundError:
                frozen.append(action)
                continue
            except OSError:
                frozen.append(action)
                continue
            try:
                proof = verify_existing_reuse(
                    self._filesystem,
                    source_path=action.source_path,
                    source_snapshot=action.source_snapshot,
                    target_path=target.as_posix(),
                )
            except (DomainViolation, OSError, ValueError):
                # Preserve TARGET_EXISTS: never assume existing content is safe
                # without a source-bound full proof and a no-follow snapshot.
                frozen.append(action)
                continue
            frozen.append(
                replace(
                    action,
                    reuse_target_snapshot=proof.target_snapshot,
                    reuse_sha256=proof.sha256,
                )
            )
        return tuple(frozen)

    def _build_actions(
        self,
        torrent_files: tuple[Any, ...],
        mapping_evidence: tuple[dict[str, Any], ...],
    ) -> tuple[
        tuple[UnpackExecutionAction, ...],
        tuple[ExecutionPlanBlockReason, ...],
    ]:
        mappings: dict[str, dict[str, Any]] = {}
        malformed = False
        for entry in mapping_evidence:
            torrent_path = entry.get("torrent_path")
            if not isinstance(torrent_path, str) or torrent_path in mappings:
                malformed = True
                continue
            mappings[torrent_path] = entry

        actions: list[UnpackExecutionAction] = []
        blockers: set[ExecutionPlanBlockReason] = set()
        if malformed:
            blockers.add(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)

        for torrent_file in torrent_files:
            if torrent_file.padding:
                actions.append(
                    UnpackExecutionAction(
                        torrent_path=torrent_file.path,
                        kind=UnpackExecutionActionKind.PROTOCOL_PADDING,
                        length=torrent_file.length,
                    )
                )
                continue
            if torrent_file.zero_length:
                actions.append(
                    UnpackExecutionAction(
                        torrent_path=torrent_file.path,
                        kind=UnpackExecutionActionKind.ZERO_LENGTH,
                        length=0,
                    )
                )
                continue

            mapping_entry = mappings.get(torrent_file.path)
            if mapping_entry is None or mapping_entry.get("state") != "MAPPED":
                blockers.add(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                continue
            source_path = mapping_entry.get("source_path")
            try:
                expected = _snapshot_from_payload(mapping_entry.get("snapshot"))
            except ValueError:
                blockers.add(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                continue
            if not isinstance(source_path, str) or expected is None:
                blockers.add(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                continue
            try:
                normalized_source = self._path_scope.normalize_reference(source_path)
                source = Path(normalized_source)
                self._path_scope.resolve_existing_directory(source.parent.as_posix())
                current = current_file_snapshot(source)
            except (DomainViolation, OSError):
                blockers.add(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                continue
            if current != expected or current.size != torrent_file.length:
                blockers.add(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                continue
            actions.append(
                UnpackExecutionAction(
                    torrent_path=torrent_file.path,
                    kind=UnpackExecutionActionKind.MATERIALIZE,
                    length=torrent_file.length,
                    source_path=source.as_posix(),
                    source_snapshot=current,
                )
            )
        return tuple(actions), tuple(sorted(blockers, key=lambda item: item.value))

    def _inspect_target_layout(
        self,
        output_root: Path,
        actions: tuple[UnpackExecutionAction, ...],
        *,
        storage_mode: TaskStorageMode,
    ) -> _TargetLayout:
        try:
            base = self._path_scope.authorization_root(output_root)
            base_stat = base.stat(follow_symlinks=False)
            root_parts = output_root.relative_to(base).parts
        except (DomainViolation, OSError, ValueError) as exc:
            raise ApplicationError(
                code="UNPACK_PLAN_TARGET_ROOT_NOT_FOUND",
                status=404,
                title="目标根目录不可用",
                detail="目标目录不在显式授权挂载范围内，或授权挂载当前不可见",
            ) from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise self._conflict("授权挂载根必须是真实目录且不能是符号链接")

        current_root = base
        device = base_stat.st_dev
        root_missing = False
        root_unsafe = False
        blockers: set[ExecutionPlanBlockReason] = set()
        for part in root_parts:
            if root_missing:
                continue
            candidate = current_root / part
            try:
                item_stat = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                root_missing = True
                continue
            except OSError:
                blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                root_unsafe = True
                break
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
                blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                root_unsafe = True
                break
            current_root = candidate
            device = item_stat.st_dev
        if root_missing:
            blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)

        directories: set[str] = set()
        for action in actions:
            relative = PurePosixPath(action.torrent_path)
            current = output_root
            missing_parent = root_missing
            unsafe_parent = root_unsafe
            for index, part in enumerate(relative.parts[:-1]):
                internal = "/".join(relative.parts[: index + 1])
                if missing_parent:
                    directories.add(internal)
                    continue
                if unsafe_parent:
                    continue
                current = current / part
                try:
                    item_stat = current.stat(follow_symlinks=False)
                except FileNotFoundError:
                    directories.add(internal)
                    missing_parent = True
                    continue
                except OSError:
                    blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                    unsafe_parent = True
                    continue
                if (
                    stat.S_ISLNK(item_stat.st_mode)
                    or not stat.S_ISDIR(item_stat.st_mode)
                    or item_stat.st_dev != device
                ):
                    blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                    unsafe_parent = True

            if not missing_parent and not unsafe_parent:
                target = output_root.joinpath(*relative.parts)
                try:
                    target_stat = target.stat(follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError:
                    blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                else:
                    if (
                        action.reuse_target_snapshot is None
                        or not stat.S_ISREG(target_stat.st_mode)
                        or (
                            target_stat.st_dev,
                            target_stat.st_ino,
                            target_stat.st_size,
                            target_stat.st_mtime_ns,
                        )
                        != (
                            action.reuse_target_snapshot.device,
                            action.reuse_target_snapshot.inode,
                            action.reuse_target_snapshot.size,
                            action.reuse_target_snapshot.mtime_ns,
                        )
                    ):
                        blockers.add(ExecutionPlanBlockReason.TARGET_EXISTS)

            if (
                storage_mode is TaskStorageMode.HARDLINK
                and action.kind is UnpackExecutionActionKind.MATERIALIZE
                and action.source_snapshot is not None
                and action.source_snapshot.device != device
            ):
                blockers.add(ExecutionPlanBlockReason.CROSS_DEVICE)

        return _TargetLayout(
            device=device,
            blocked_reasons=tuple(sorted(blockers, key=lambda item: item.value)),
            create_directories=tuple(sorted(directories)),
        )

    def _persist_plan(self, context: _PlanContext, plan: UnpackExecutionPlan) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, context.item_id)
            candidate = session.get(UnpackMatchCandidate, context.candidate_id)
            execution = session.get(UnpackExecution, context.execution_id)
            if (
                item is None
                or candidate is None
                or execution is None
                or item.status != UnpackItemStatus.CONTENT_VERIFIED.value
                or item.version != context.item_version
                or item.selected_candidate_id != context.candidate_id
                or item.candidate_generation != context.generation
                or candidate.metainfo_digest != context.metainfo_digest
            ):
                raise self._conflict("生成执行计划期间 item/candidate 已变化")
            payload = unpack_execution_plan_to_payload(plan)
            now = utc_now()
            item.execution_plan = payload
            item.execution_plan_digest = plan.plan_digest
            item.execution_plan_created_at = now
            item.status = UnpackItemStatus.PLAN_PENDING.value
            item.last_error_code = None if plan.ready else "UNPACK_EXECUTION_PLAN_BLOCKED"
            item.last_error_message = (
                None
                if plan.ready
                else "执行计划被安全门阻断："
                + ",".join(item.value for item in plan.blocked_reasons)
            )
            item.updated_at = now
            item.version += 1
            execution.status = UnpackExecutionStatus.EXECUTING.value
            execution.updated_at = now
            execution.version += 1
            session.commit()

    def _mark_error(self, item_id: str, *, code: str, message: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None or item.status != UnpackItemStatus.CONTENT_VERIFIED.value:
                return
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                return
            now = utc_now()
            item.status = UnpackItemStatus.PLAN_PENDING.value
            session.flush()
            item.status = UnpackItemStatus.EXECUTION_ERROR.value
            item.last_error_code = code
            item.last_error_message = message
            item.updated_at = now
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _report(
        self,
        execution_id: str,
        *,
        processed_count: int,
    ) -> UnpackExecutionPlanningReport:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()

            def count(status: UnpackItemStatus) -> int:
                return int(
                    session.scalar(
                        select(func.count(UnpackExecutionItem.id))
                        .where(UnpackExecutionItem.execution_id == execution_id)
                        .where(UnpackExecutionItem.status == status.value)
                    )
                    or 0
                )

            blocked_count = int(
                session.scalar(
                    select(func.count(UnpackExecutionItem.id))
                    .where(UnpackExecutionItem.execution_id == execution_id)
                    .where(UnpackExecutionItem.status == UnpackItemStatus.PLAN_PENDING.value)
                    .where(UnpackExecutionItem.last_error_code == "UNPACK_EXECUTION_PLAN_BLOCKED")
                )
                or 0
            )
            ready_count = count(UnpackItemStatus.PLAN_PENDING) - blocked_count
            return UnpackExecutionPlanningReport(
                execution_id=execution_id,
                processed_count=processed_count,
                ready_count=ready_count,
                blocked_count=blocked_count,
                error_count=count(UnpackItemStatus.EXECUTION_ERROR),
                execution_status=UnpackExecutionStatus(execution.status),
            )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_PLAN_INVALID",
            status=422,
            title="数据拆包执行计划参数无效",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_NOT_FOUND",
            status=404,
            title="数据拆包执行不存在",
            detail="未找到指定数据拆包执行",
        )

    @staticmethod
    def _item_not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_NOT_FOUND",
            status=404,
            title="数据拆包影片项不存在",
            detail="未找到指定影片项",
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_PLAN_CONFLICT",
            status=409,
            title="数据拆包执行计划状态冲突",
            detail=detail,
        )


def _snapshot_from_payload(value: object) -> FileSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("文件快照必须是对象")
    device = value.get("device")
    inode = value.get("inode")
    size = value.get("size")
    mtime_ns = value.get("mtime_ns")
    file_type = value.get("file_type")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not isinstance(mtime_ns, (str, int))
        or not isinstance(file_type, str)
    ):
        raise ValueError("文件快照字段无效")
    return FileSnapshot(
        device=device,
        inode=inode,
        size=size,
        mtime_ns=int(mtime_ns),
        file_type=file_type,
    )


def _safe_error(
    exc: ApplicationError | SiteAdapterError | DomainViolation | ValueError,
) -> tuple[str, str]:
    if isinstance(exc, ApplicationError):
        return exc.code, exc.detail
    if isinstance(exc, SiteAdapterError):
        return exc.code, "生成执行计划时访问站点失败"
    if isinstance(exc, DomainViolation):
        return exc.code.value, "生成执行计划时路径安全校验失败"
    return "UNPACK_EXECUTION_PLAN_INVALID", str(exc)


def _refresh_execution_state(session: Session, execution: UnpackExecution) -> None:
    def count(status: UnpackItemStatus) -> int:
        return int(
            session.scalar(
                select(func.count(UnpackExecutionItem.id))
                .where(UnpackExecutionItem.execution_id == execution.id)
                .where(UnpackExecutionItem.status == status.value)
            )
            or 0
        )

    execution.content_verified_count = count(UnpackItemStatus.CONTENT_VERIFIED)
    execution.error_count = count(UnpackItemStatus.MATCH_ERROR) + count(
        UnpackItemStatus.EXECUTION_ERROR
    )
    execution.completed_count = count(UnpackItemStatus.COMPLETED)
    active_execution = sum(
        count(status)
        for status in (
            UnpackItemStatus.CONTENT_VERIFIED,
            UnpackItemStatus.PLAN_PENDING,
            UnpackItemStatus.EXECUTING,
            UnpackItemStatus.CLIENT_VERIFYING,
        )
    )
    now = utc_now()
    if active_execution:
        execution.status = UnpackExecutionStatus.EXECUTING.value
        execution.finished_at = None
    elif execution.error_count:
        execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
        execution.finished_at = now
    elif execution.completed_count == execution.total_count:
        execution.status = UnpackExecutionStatus.COMPLETED.value
        execution.finished_at = now
    execution.updated_at = now
    execution.version += 1
