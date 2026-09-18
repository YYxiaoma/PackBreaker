from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.backup_schedule import BackupDriver
from backend.app.application.downloaders import DownloaderService
from backend.app.application.errors import ApplicationError
from backend.app.application.notification_driver import NotificationDriver
from backend.app.application.sites import SiteService
from backend.app.application.system_health import SystemHealthService
from backend.app.application.system_upgrades import SystemUpgradeService
from backend.app.application.task_definitions import TaskDefinitionService
from backend.app.application.task_driver import ActiveTaskDriver
from backend.app.config import AppSettings
from backend.app.domain.ai_agent import AI_TOOL_SCOPES, AIDataScope, AIToolName
from backend.app.infrastructure.app_logging import sanitize_message
from backend.app.infrastructure.operational_logs import (
    MAX_LOG_WINDOW_MINUTES,
    OperationalLogLevel,
    query_operational_logs,
)
from backend.app.infrastructure.persistence.models import (
    TaskExecution,
    TaskExecutionEvent,
    TaskExecutionItem,
)
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.site_reliability import SiteReliabilityRegistry

_MAX_TOOL_ITEMS = 50
_MAX_LOG_ITEMS = 50
_MAX_HELP_RESULTS = 10


@dataclass(frozen=True, slots=True)
class AIToolResult:
    name: AIToolName
    scope: AIDataScope
    payload: dict[str, object]


class AIToolService:
    """AI 只能通过这里的显式只读白名单读取 PackBreaker 数据。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: AppSettings,
        runtime: RuntimeManager,
        site_reliability_registry: SiteReliabilityRegistry,
        task_definition_service: TaskDefinitionService,
        downloader_service: DownloaderService,
        site_service: SiteService,
        system_upgrade_service: SystemUpgradeService,
        task_driver: ActiveTaskDriver,
        notification_driver: NotificationDriver,
        backup_driver: BackupDriver,
        help_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._runtime = runtime
        self._site_reliability_registry = site_reliability_registry
        self._task_definition_service = task_definition_service
        self._downloader_service = downloader_service
        self._site_service = site_service
        self._system_upgrade_service = system_upgrade_service
        self._task_driver = task_driver
        self._notification_driver = notification_driver
        self._backup_driver = backup_driver
        self._help_root = help_root.resolve(strict=False)

    @staticmethod
    def available_tools(scopes: tuple[AIDataScope, ...]) -> tuple[AIToolName, ...]:
        allowed = set(scopes)
        return tuple(tool for tool in AIToolName if AI_TOOL_SCOPES[tool] in allowed)

    async def execute(
        self,
        name: AIToolName,
        arguments: Mapping[str, object],
        *,
        allowed_scopes: tuple[AIDataScope, ...],
    ) -> AIToolResult:
        scope = AI_TOOL_SCOPES[name]
        if scope not in allowed_scopes:
            raise ApplicationError(
                code="AI_TOOL_SCOPE_DENIED",
                status=403,
                title="AI 工具读取范围未授权",
                detail="当前 AI 数据权限不允许使用该只读工具",
            )
        if name is AIToolName.GET_SYSTEM_HEALTH:
            payload = await self._get_system_health(arguments)
        elif name is AIToolName.GET_VERSION_STATUS:
            payload = await self._get_version_status(arguments)
        elif name is AIToolName.LIST_TASK_DEFINITIONS:
            payload = self._list_task_definitions(arguments)
        elif name is AIToolName.GET_TASK_EXECUTION:
            payload = self._get_task_execution(arguments)
        elif name is AIToolName.QUERY_REDACTED_LOGS:
            payload = self._query_redacted_logs(arguments)
        elif name is AIToolName.GET_SITE_STATUS:
            payload = await self._get_site_status(arguments)
        elif name is AIToolName.GET_DOWNLOADER_STATUS:
            payload = await self._get_downloader_status(arguments)
        elif name is AIToolName.SEARCH_HELP_DOCS:
            payload = self._search_help_docs(arguments)
        else:  # pragma: no cover - StrEnum exhaustiveness guard
            raise AssertionError(f"未处理的 AI Tool: {name}")
        return AIToolResult(name=name, scope=scope, payload=payload)

    async def _get_system_health(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, set())
        report = await SystemHealthService(
            self._session_factory,
            settings=self._settings,
            runtime=self._runtime,
            site_reliability_registry=self._site_reliability_registry,
        ).snapshot(
            task_driver=self._task_driver.state,
            notification_driver=self._notification_driver.state,
            backup_driver=self._backup_driver.state,
        )
        return report.as_dict()

    async def _get_version_status(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, set())
        status = await self._system_upgrade_service.status()
        return {
            "current_version": status.current_version,
            "latest_version": status.latest_version,
            "update_available": status.update_available,
            "release_error_code": status.release_error_code,
            "can_upgrade": status.can_upgrade,
            "blocked_reasons": list(status.blocked_reasons),
        }

    def _list_task_definitions(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, {"limit"})
        limit = self._bounded_int(
            arguments.get("limit"), default=20, minimum=1, maximum=_MAX_TOOL_ITEMS
        )
        definitions = self._task_definition_service.list_definitions()[:limit]
        return {
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": item.kind,
                    "status": item.status,
                    "site_name": item.site_name,
                    "site_available": item.site_available,
                    "source_kind": item.source_kind,
                    "source_downloader_name": item.source_downloader_name,
                    "source_available": item.source_available,
                    "cron_expression": item.cron_expression,
                    "timezone": item.timezone,
                    "next_run_at": self._timestamp(item.next_run_at),
                    "latest_execution": (
                        None
                        if item.latest_execution is None
                        else {
                            "id": item.latest_execution.id,
                            "status": item.latest_execution.status,
                            "phase": item.latest_execution.phase,
                            "success_count": item.latest_execution.success_count,
                            "failed_count": item.latest_execution.failed_count,
                            "skipped_count": item.latest_execution.skipped_count,
                            "finished_at": self._timestamp(item.latest_execution.finished_at),
                        }
                    ),
                }
                for item in definitions
            ],
            "count": len(definitions),
        }

    def _get_task_execution(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, {"execution_id"})
        execution_id = self._required_text(arguments.get("execution_id"), "execution_id", 36)
        with self._session_factory() as session:
            execution = session.get(TaskExecution, execution_id)
            if execution is None:
                raise ApplicationError(
                    code="AI_TOOL_TASK_EXECUTION_NOT_FOUND",
                    status=404,
                    title="任务执行记录不存在",
                    detail="未找到指定任务执行记录",
                )
            items = list(
                session.scalars(
                    select(TaskExecutionItem)
                    .where(TaskExecutionItem.execution_id == execution_id)
                    .order_by(TaskExecutionItem.created_at.desc())
                    .limit(_MAX_TOOL_ITEMS)
                )
            )
            events = list(
                session.scalars(
                    select(TaskExecutionEvent)
                    .where(TaskExecutionEvent.execution_id == execution_id)
                    .order_by(TaskExecutionEvent.created_at.desc())
                    .limit(_MAX_TOOL_ITEMS)
                )
            )
        return {
            "execution": {
                "id": execution.id,
                "task_definition_id": execution.task_definition_id,
                "task_name": execution.task_name,
                "trigger": execution.trigger,
                "status": execution.status,
                "phase": execution.phase,
                "discovered_count": execution.discovered_count,
                "success_count": execution.success_count,
                "failed_count": execution.failed_count,
                "skipped_count": execution.skipped_count,
                "started_at": self._timestamp(execution.started_at),
                "finished_at": self._timestamp(execution.finished_at),
                "created_at": self._timestamp(execution.created_at),
            },
            "items": [
                {
                    "name": item.name[:256],
                    "size_bytes": item.size_bytes,
                    "phase": item.phase,
                    "progress": item.progress,
                    "result": item.result,
                    "error_code": item.error_code,
                    "error_summary": (
                        None
                        if item.error_summary_zh is None
                        else sanitize_message(item.error_summary_zh)[:1024]
                    ),
                    "retryable": item.retryable,
                    "retry_count": item.retry_count,
                }
                for item in items
            ],
            "events": [
                {
                    "event_code": event.event_code,
                    "message": sanitize_message(event.message)[:1024],
                    "created_at": self._timestamp(event.created_at),
                }
                for event in events
            ],
        }

    def _query_redacted_logs(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, {"window_minutes", "limit", "level", "query"})
        window = self._bounded_int(
            arguments.get("window_minutes"), default=60, minimum=1, maximum=MAX_LOG_WINDOW_MINUTES
        )
        limit = self._bounded_int(
            arguments.get("limit"), default=20, minimum=1, maximum=_MAX_LOG_ITEMS
        )
        query = self._optional_text(arguments.get("query"), "query", 128)
        raw_level = self._optional_text(arguments.get("level"), "level", 16)
        level: OperationalLogLevel | None = None
        if raw_level is not None:
            if raw_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise self._invalid_arguments("level 不是允许的日志级别")
            level = raw_level  # type: ignore[assignment]
        result = query_operational_logs(
            self._settings.log_dir,
            backup_count=self._settings.log_file_backup_count,
            max_file_bytes=self._settings.log_file_max_bytes,
            window_minutes=window,
            limit=limit,
            level=level,
            query=query,
        )
        return result.as_dict()

    async def _get_site_status(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, set())
        result: list[dict[str, object]] = []
        for site in self._site_service.list_sites()[:_MAX_TOOL_ITEMS]:
            health = await self._site_service.health(site.id)
            result.append(
                {
                    "id": site.id,
                    "name": site.name,
                    "type": site.type.value,
                    "connection_status": site.connection_status.value,
                    "enabled": site.enabled,
                    "last_test_at": self._timestamp(site.last_test_at),
                    "reliability_state": health.circuit_state,
                    "consecutive_failures": health.failure_count,
                    "retry_after_seconds": health.retry_after_seconds,
                }
            )
        return {"items": result, "count": len(result)}

    async def _get_downloader_status(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, {"include_metrics"})
        include_metrics = arguments.get("include_metrics", True)
        if not isinstance(include_metrics, bool):
            raise self._invalid_arguments("include_metrics 必须是布尔值")
        result: list[dict[str, object]] = []
        for downloader in self._downloader_service.list_downloaders()[:_MAX_TOOL_ITEMS]:
            metrics: dict[str, object] | None = None
            metric_error: str | None = None
            if include_metrics and downloader.enabled:
                try:
                    value = await self._downloader_service.runtime_metrics(downloader.id)
                    metrics = {
                        "upload_speed_bytes_per_second": value.upload_speed_bytes_per_second,
                        "download_speed_bytes_per_second": value.download_speed_bytes_per_second,
                        "total_content_size_bytes": value.total_content_size_bytes,
                        "free_space_bytes": value.free_space_bytes,
                        "active_torrent_count": value.active_torrent_count,
                        "total_torrent_count": value.total_torrent_count,
                        "sampled_at": self._timestamp(value.sampled_at),
                    }
                except ApplicationError as exc:
                    metric_error = exc.code
            result.append(
                {
                    "id": downloader.id,
                    "name": downloader.name,
                    "type": downloader.type.value,
                    "connection_status": downloader.connection_status.value,
                    "path_mapping_status": downloader.path_mapping_status.value,
                    "enabled": downloader.enabled,
                    "last_test_at": self._timestamp(downloader.last_test_at),
                    "metrics": metrics,
                    "metrics_error_code": metric_error,
                }
            )
        return {"items": result, "count": len(result)}

    def _search_help_docs(self, arguments: Mapping[str, object]) -> dict[str, object]:
        self._require_keys(arguments, {"query"})
        query = self._required_text(arguments.get("query"), "query", 80)
        needle = query.casefold()
        candidates = [self._help_root / "README.md"]
        docs_dir = self._help_root / "docs"
        if docs_dir.is_dir():
            candidates.extend(sorted(docs_dir.glob("*.md")))
        items: list[dict[str, object]] = []
        for path in candidates[:30]:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self._help_root)
                lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, ValueError):
                continue
            for index, line in enumerate(lines):
                if needle not in line.casefold():
                    continue
                start = max(0, index - 1)
                end = min(len(lines), index + 2)
                excerpt = "\n".join(lines[start:end])[:800]
                items.append(
                    {
                        "document": resolved.relative_to(self._help_root).as_posix(),
                        "line": index + 1,
                        "excerpt": excerpt,
                    }
                )
                if len(items) >= _MAX_HELP_RESULTS:
                    return {"items": items, "count": len(items)}
        return {"items": items, "count": len(items)}

    @staticmethod
    def _require_keys(arguments: Mapping[str, object], allowed: set[str]) -> None:
        unknown = set(arguments) - allowed
        if unknown:
            raise AIToolService._invalid_arguments("AI Tool 参数包含未声明字段")

    @staticmethod
    def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise AIToolService._invalid_arguments(f"整数参数必须位于 {minimum}..{maximum}")
        return value

    @staticmethod
    def _required_text(value: object, name: str, max_length: int) -> str:
        normalized = AIToolService._optional_text(value, name, max_length)
        if normalized is None:
            raise AIToolService._invalid_arguments(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _optional_text(value: object, name: str, max_length: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise AIToolService._invalid_arguments(f"{name} 必须是字符串")
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > max_length or any(c in normalized for c in ("\r", "\n", "\x00")):
            raise AIToolService._invalid_arguments(f"{name} 格式无效")
        return normalized

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _invalid_arguments(detail: str) -> ApplicationError:
        return ApplicationError(
            code="AI_TOOL_ARGUMENT_INVALID",
            status=422,
            title="AI Tool 参数无效",
            detail=detail,
        )
