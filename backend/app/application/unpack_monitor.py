from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_definition import next_cron_run
from backend.app.domain.unpack import (
    UnpackDefinitionStatus,
    UnpackExecutionStatus,
    UnpackExecutionTrigger,
    UnpackSourceKind,
    UnpackTriggerKind,
)
from backend.app.infrastructure.adapters.downloaders import DownloaderTorrent
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    new_uuid,
    utc_now,
)


class UnpackDownloaderBindingPort(Protocol):
    def container_path(self, remote_path: str) -> Path: ...


class UnpackDownloaderMonitorPort(Protocol):
    async def list_all_torrents(self, downloader_id: str) -> tuple[DownloaderTorrent, ...]: ...

    def write_binding(self, downloader_id: str) -> UnpackDownloaderBindingPort: ...


@dataclass(frozen=True, slots=True)
class UnpackMonitorTriggerResult:
    definition_id: str
    execution_id: str | None
    next_run_at: datetime
    skipped_overlap: bool


class UnpackMonitorService:
    """监控拆包调度：冻结一次触发的来源边界，然后交给统一 discovery。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: UnpackDownloaderMonitorPort,
    ) -> None:
        self._session_factory = session_factory
        self._downloader_service = downloader_service

    def list_due_definition_ids(
        self,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("监控任务查询数量必须位于 1 到 500 之间")
        current = now or utc_now()
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackDefinition.id)
                    .where(UnpackDefinition.trigger_kind == UnpackTriggerKind.MONITOR.value)
                    .where(UnpackDefinition.status == UnpackDefinitionStatus.ENABLED.value)
                    .where(UnpackDefinition.next_run_at.is_not(None))
                    .where(UnpackDefinition.next_run_at <= current)
                    .order_by(UnpackDefinition.next_run_at, UnpackDefinition.id)
                    .limit(limit)
                ).all()
            )

    async def trigger_due_definition(
        self,
        definition_id: str,
        *,
        now: datetime | None = None,
    ) -> UnpackMonitorTriggerResult:
        current = now or utc_now()
        snapshot = self._load_trigger_snapshot(definition_id, now=current)
        downloader_sources: list[dict[str, Any]] | None = None
        if snapshot["source_kind"] == UnpackSourceKind.DOWNLOADER.value:
            downloader_sources = await self._freeze_downloader_sources(snapshot)

        with self._session_factory() as session:
            begin_immediate_write(session)
            definition = session.get(UnpackDefinition, definition_id)
            if definition is None:
                raise self._not_found()
            if (
                definition.trigger_kind != UnpackTriggerKind.MONITOR.value
                or definition.status != UnpackDefinitionStatus.ENABLED.value
                or definition.next_run_at is None
                or definition.next_run_at > current
            ):
                raise self._conflict("监控任务当前未到执行时间或已不再启用")
            if definition.version != snapshot["definition_version"]:
                raise self._conflict("监控任务配置在触发准备期间已变化，请等待下一轮调度")

            cron_expression = definition.cron_expression
            timezone = definition.timezone
            if not cron_expression or not timezone:
                raise self._conflict("监控任务缺少有效 Cron 或时区配置")

            next_run = next_cron_run(cron_expression, current, timezone=timezone)
            running = session.scalar(
                select(UnpackExecution.id)
                .where(UnpackExecution.definition_id == definition.id)
                .where(
                    UnpackExecution.status.not_in(
                        (
                            UnpackExecutionStatus.COMPLETED.value,
                            UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value,
                            UnpackExecutionStatus.FAILED.value,
                            UnpackExecutionStatus.CANCELLED.value,
                        )
                    )
                )
                .limit(1)
            )
            if running is not None:
                definition.next_run_at = next_run
                definition.updated_at = current
                definition.version += 1
                session.commit()
                return UnpackMonitorTriggerResult(
                    definition_id=definition.id,
                    execution_id=None,
                    next_run_at=next_run,
                    skipped_overlap=True,
                )

            config_snapshot = {
                "definition_id": definition.id,
                "definition_version": definition.version,
                "name": definition.name,
                "trigger_kind": definition.trigger_kind,
                "source_kind": definition.source_kind,
                "execution_scope_kind": definition.execution_scope_kind,
                "source_config": dict(definition.source_config),
                "file_filter": dict(definition.file_filter),
                "site_ids": list(definition.site_ids),
                "output_config": dict(definition.output_config),
                "retry": {
                    "enabled": definition.retry_enabled,
                    "max_retries": definition.max_retries,
                },
                "matching": {
                    "auto_match_threshold_bps": definition.auto_match_threshold_bps,
                },
                "selected_sources": [],
            }
            if downloader_sources is not None:
                config_snapshot["downloader_sources"] = downloader_sources

            execution = UnpackExecution(
                id=new_uuid(),
                definition_id=definition.id,
                trigger=UnpackExecutionTrigger.SCHEDULE.value,
                status=UnpackExecutionStatus.DISCOVERING.value,
                config_snapshot=config_snapshot,
                discovery_complete=False,
                total_count=0,
                matched_auto_count=0,
                review_count=0,
                content_verified_count=0,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=0,
                started_at=current,
                version=1,
                created_at=current,
                updated_at=current,
            )
            session.add(execution)
            definition.last_triggered_at = current
            definition.next_run_at = next_run
            definition.updated_at = current
            definition.version += 1
            session.commit()
            return UnpackMonitorTriggerResult(
                definition_id=definition.id,
                execution_id=execution.id,
                next_run_at=next_run,
                skipped_overlap=False,
            )

    def _load_trigger_snapshot(self, definition_id: str, *, now: datetime) -> dict[str, Any]:
        with self._session_factory() as session:
            definition = session.get(UnpackDefinition, definition_id)
            if definition is None:
                raise self._not_found()
            if (
                definition.trigger_kind != UnpackTriggerKind.MONITOR.value
                or definition.status != UnpackDefinitionStatus.ENABLED.value
                or definition.next_run_at is None
                or definition.next_run_at > now
            ):
                raise self._conflict("监控任务当前未到执行时间或已不再启用")
            return {
                "definition_version": definition.version,
                "source_kind": definition.source_kind,
                "source_config": dict(definition.source_config),
            }

    async def _freeze_downloader_sources(
        self,
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        source_config = snapshot["source_config"]
        downloader_id = source_config.get("downloader_id")
        if not isinstance(downloader_id, str) or not downloader_id:
            raise self._conflict("监控任务下载器来源缺少 downloader_id")
        torrents = await self._downloader_service.list_all_torrents(downloader_id)
        binding = self._downloader_service.write_binding(downloader_id)
        frozen: list[dict[str, Any]] = []
        for torrent in sorted(torrents, key=lambda item: (item.torrent_hash, item.name.casefold())):
            if torrent.progress < 1.0:
                continue
            if not _monitor_torrent_matches(source_config, torrent):
                continue
            remote_content = torrent.content_path or torrent.save_path
            item: dict[str, Any] = {
                "torrent_hash": torrent.torrent_hash,
                "name": torrent.name,
                "size_bytes": torrent.size_bytes,
                "category": torrent.category,
                "tags": list(torrent.tags),
                "remote_content": remote_content,
            }
            try:
                item["mapped_content_path"] = binding.container_path(remote_content).as_posix()
            except (ApplicationError, DomainViolation, OSError, ValueError) as exc:
                item["mapping_error_code"] = "UNPACK_DOWNLOADER_PATH_MAPPING_FAILED"
                item["mapping_error_message"] = str(exc)
            frozen.append(item)
        return frozen

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MONITOR_INVALID",
            status=422,
            title="监控拆包调度参数无效",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_DEFINITION_NOT_FOUND",
            status=404,
            title="数据拆包任务不存在",
            detail="未找到指定数据拆包任务",
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MONITOR_CONFLICT",
            status=409,
            title="监控拆包调度状态冲突",
            detail=detail,
        )


def _monitor_torrent_matches(config: dict[str, Any], torrent: DownloaderTorrent) -> bool:
    raw_name = config.get("name_contains")
    if isinstance(raw_name, str):
        name_contains = raw_name.strip().casefold()
        if name_contains and name_contains not in torrent.name.casefold():
            return False

    raw_categories = config.get("categories", [])
    if isinstance(raw_categories, list):
        categories = {
            value.strip().casefold()
            for value in raw_categories
            if isinstance(value, str) and value.strip()
        }
        if categories and (torrent.category or "").strip().casefold() not in categories:
            return False

    raw_tags = config.get("tags", [])
    if isinstance(raw_tags, list):
        tags = {
            value.strip().casefold()
            for value in raw_tags
            if isinstance(value, str) and value.strip()
        }
        torrent_tags = {value.strip().casefold() for value in torrent.tags if value.strip()}
        if tags and not tags & torrent_tags:
            return False
    return True
