"""做种健康巡查应用服务。

数据来源（不可混淆）：
- 下载器：种子列表、做种时长、tracker 状态、文件路径（``list_all_torrents``）。
- 站点：仅用详情页 404 作为「站侧已无记录」的唯一可信信号（``fetch_details``）。
- 文件系统：``content_path`` / ``save_path`` 在容器内的可见性。

判定遵循「宁可漏报，不可误报」：站点不可达、路径不可见、规则未知一律记为
UNKNOWN，UNKNOWN 既不会新建异常，也不会清除已有异常。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.downloader import PathMappingRule, map_remote_path
from backend.app.domain.operation import OperationStatus
from backend.app.domain.seeding_health import (
    ACTIVE_ISSUE_STATUSES,
    HNR_WARNING_HOURS,
    SeedingIssueKind,
    SeedingIssueResolution,
    SeedingIssueStatus,
    SeedingRunStatus,
    SeedingRunTrigger,
    SeedingWatchPresence,
    SiteProbeOutcome,
    SiteProbeTally,
    hnr_remaining_hours,
    is_site_degraded,
    risk_score,
    severity_for,
    should_confirm_missing,
    status_on_clear,
    status_on_redetect,
    suggestion_for,
)
from backend.app.domain.torrent_identity import identity_from_comment
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    QbittorrentAddRequest,
    TransmissionAddRequest,
)
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    SeedingHealthConfig,
    SeedingHealthIssue,
    SeedingHealthRun,
    SeedingPolicy,
    SeedingRepairJournal,
    SeedingWatch,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

_SITE_CONCURRENCY = 4
# 首次铺开时每个种子要导出一次 .torrent；实测 7251 个种子需要分多轮完成，
# 上限过高会长时间占用下载器，过低则站点侧判定要等好几天才生效。
_MAX_IDENTITY_RESOLUTION_PER_RUN = 1500
_SEEDING_TOLERANCE = 0.999
_STALE_AFTER_HOURS = 12
# 单站最多翻 40 页（4000 条）。超过就判定列表未取全，跳过 SITE_ONLY。
_MAX_SITE_ONLY_PAGES = 40
# 本地识别出的该站种子数不足站点列表的这个比例时，差集主要是「未解析身份」
# 而不是「站点独有」，此时必须放弃判定，否则整站会被误报一遍。
_SITE_ONLY_MIN_COVERAGE = 0.8
_CONFIG_ID = "default"

_ISSUE_LABEL: dict[str, str] = {
    SeedingIssueKind.SITE_MISSING.value: "站侧无记录",
    SeedingIssueKind.SITE_ONLY.value: "站侧有但下载器没有",
    SeedingIssueKind.TRACKER_ERROR.value: "Tracker 持续报错",
    SeedingIssueKind.FILE_MISSING.value: "本地文件缺失",
}


class DownloaderDirectory(Protocol):
    def list_downloaders(self) -> Sequence[Any]: ...

    async def list_all_torrents(self, downloader_id: str) -> tuple[Any, ...]: ...

    def write_binding(self, downloader_id: str) -> Any: ...


class SiteDirectory(Protocol):
    def enabled_adapters(self) -> tuple[Any, ...]: ...


@dataclass(frozen=True, slots=True)
class SeedingHealthSettings:
    interval_hours: int = 6
    confirm_threshold: int = 2
    detail_ttl_hours: int = 72
    degrade_ratio: float = 0.5
    tracker_batch: int = 50
    max_watches: int = 20000
    # 单站点每轮最多探测多少个详情页。大库必须靠轮转，否则会被站点风控。
    site_probe_budget: int = 20
    # 站点侧核对默认关闭：7255 个种子 × 17 个站的请求间隔决定它必然很慢，
    # 默认只跑「文件 + tracker」才能保持几十秒出结果。
    site_probe_enabled: bool = False
    run_timeout_seconds: float = 1200.0


@dataclass(frozen=True, slots=True)
class _WatchTarget:
    watch_id: str
    downloader_id: str
    torrent_hash: str
    torrent_name: str
    size_bytes: int
    site_key: str | None
    remote_torrent_id: str | None
    site_outcome: SiteProbeOutcome
    checked_at: datetime | None
    consecutive_misses: int
    content_path: str | None
    save_path: str
    seeding_time_seconds: int | None
    tracker_state: str | None
    tracker_message: str | None
    tracker_checked_at: datetime | None


class SeedingHealthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: DownloaderDirectory,
        site_service: SiteDirectory,
        *,
        settings: SeedingHealthSettings | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._downloaders = downloader_service
        self._sites = site_service
        self._settings = settings or SeedingHealthSettings()
        self._logger = logger or logging.getLogger("packbreaker.seeding_health")

    # ------------------------------------------------------------------ 读接口
    def summary(self) -> dict[str, Any]:
        active = [item.value for item in ACTIVE_ISSUE_STATUSES]
        with self._session_factory() as session:
            presence_counts: dict[str, int] = {
                str(row[0]): int(row[1])
                for row in session.execute(
                    select(SeedingWatch.presence, func.count()).group_by(SeedingWatch.presence)
                ).all()
            }
            watched = presence_counts.get(
                SeedingWatchPresence.ACTIVE.value, 0
            ) + presence_counts.get(SeedingWatchPresence.SITE_ONLY.value, 0)
            by_kind = {
                kind: int(count)
                for kind, count in session.execute(
                    select(
                        SeedingHealthIssue.kind,
                        func.count(func.distinct(SeedingHealthIssue.watch_id)),
                    )
                    .where(SeedingHealthIssue.status.in_(active))
                    .group_by(SeedingHealthIssue.kind)
                ).all()
            }
            abnormal = (
                session.scalar(
                    select(func.count(func.distinct(SeedingHealthIssue.watch_id))).where(
                        SeedingHealthIssue.status.in_(active)
                    )
                )
                or 0
            )
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(SeedingHealthIssue)
                    .where(SeedingHealthIssue.status == SeedingIssueStatus.OPEN.value)
                )
                or 0
            )
            hnr_risk = (
                session.scalar(
                    select(func.count(func.distinct(SeedingHealthIssue.watch_id))).where(
                        SeedingHealthIssue.status.in_(active),
                        SeedingHealthIssue.hnr_remaining_hours.is_not(None),
                        SeedingHealthIssue.hnr_remaining_hours < HNR_WARNING_HOURS,
                    )
                )
                or 0
            )
            active_watch = SeedingWatch.presence == SeedingWatchPresence.ACTIVE.value
            identity_count = (
                session.scalar(
                    select(func.count())
                    .select_from(SeedingWatch)
                    .where(active_watch, SeedingWatch.site_key.is_not(None))
                )
                or 0
            )
            tracker_count = (
                session.scalar(
                    select(func.count())
                    .select_from(SeedingWatch)
                    .where(active_watch, SeedingWatch.tracker_state.is_not(None))
                )
                or 0
            )
            site_checked_count = (
                session.scalar(
                    select(func.count())
                    .select_from(SeedingWatch)
                    .where(active_watch, SeedingWatch.site_checked_at.is_not(None))
                )
                or 0
            )
            unmapped_count = self._unmapped_count(session)
            last_run = session.scalars(
                select(SeedingHealthRun).order_by(SeedingHealthRun.started_at.desc()).limit(1)
            ).first()
            next_run_at = None
            if last_run is not None and last_run.status != SeedingRunStatus.RUNNING.value:
                candidate = last_run.started_at + timedelta(hours=self._settings.interval_hours)
                # 调度停摆时不要显示一个已经过去的时间点。
                next_run_at = candidate if candidate > utc_now() else None

        return {
            "watched": watched,
            "healthy": max(watched - abnormal, 0),
            "abnormal": abnormal,
            "hnr_risk": hnr_risk,
            "pending": pending,
            "recovered": int(last_run.resolved_issues) if last_run else 0,
            "by_kind": by_kind,
            "last_run": _run_view(last_run) if last_run else None,
            "next_run_at": next_run_at.isoformat() if next_run_at else None,
            "stale": _is_stale(last_run),
            "interval_hours": self._settings.interval_hours,
            # 让「为什么只有两条异常」可解释：三类检查各自覆盖了多少。
            "coverage": {
                "files": watched - unmapped_count,
                "unmapped": unmapped_count,
                "tracker": tracker_count,
                "site": site_checked_count,
                "identity": identity_count,
            },
        }

    def _unmapped_count(self, session: Session) -> int:
        """统计路径映射不到、因而无法做文件检查的种子数。

        这类种子必须显式报出来，否则「没有文件缺失」会被误读成「文件都正常」。
        """
        rules: dict[str, list[PathMappingRule]] = {}
        for downloader in self._downloaders.list_downloaders():
            rules[downloader.id] = list(downloader.path_mappings)
        count = 0
        rows = session.execute(
            select(SeedingWatch.downloader_id, SeedingWatch.save_path).where(
                SeedingWatch.presence == SeedingWatchPresence.ACTIVE.value
            )
        ).all()
        for downloader_id, save_path in rows:
            if _map_path(save_path, rules.get(downloader_id, [])) is None:
                count += 1
        return count

    def list_issues(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        site_key: str | None = None,
        downloader_id: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        if page < 1 or not 1 <= page_size <= 200:
            raise ApplicationError(
                code="SEEDING_HEALTH_PAGE_INVALID",
                status=422,
                title="分页参数无效",
                detail="page 必须大于等于 1，page_size 必须位于 1..200",
            )
        active = [item.value for item in ACTIVE_ISSUE_STATUSES]
        statement = select(SeedingHealthIssue, SeedingWatch).join(
            SeedingWatch, SeedingWatch.id == SeedingHealthIssue.watch_id
        )
        if status and status.upper() != "ALL":
            statement = statement.where(SeedingHealthIssue.status == status.upper())
        elif not status:
            statement = statement.where(SeedingHealthIssue.status.in_(active))
        if kind:
            statement = statement.where(SeedingHealthIssue.kind == kind)
        if severity:
            statement = statement.where(SeedingHealthIssue.severity == severity)
        if site_key:
            statement = statement.where(SeedingWatch.site_key == site_key)
        if downloader_id:
            statement = statement.where(SeedingWatch.downloader_id == downloader_id)
        needle = (search or "").strip()
        if needle:
            statement = statement.where(SeedingWatch.torrent_name.contains(needle))

        with self._session_factory() as session:
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.execute(
                statement.order_by(
                    SeedingHealthIssue.risk_score.desc(),
                    SeedingHealthIssue.last_detected_at.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            items = [_issue_row(issue, watch) for issue, watch in rows]
        return {"items": items, "total": int(total), "page": page, "page_size": page_size}

    def issue_detail(self, issue_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            issue = session.get(SeedingHealthIssue, issue_id)
            watch = session.get(SeedingWatch, issue.watch_id) if issue is not None else None
            if issue is None or watch is None:
                raise _not_found()
            return _issue_detail(issue, watch)

    def list_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SeedingHealthRun).order_by(SeedingHealthRun.started_at.desc()).limit(limit)
            ).all()
        return [_run_view(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            run = session.get(SeedingHealthRun, run_id)
            if run is None:
                raise _not_found()
            return _run_view(run)

    def get_settings(self) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(SeedingHealthConfig, _CONFIG_ID)
        return {
            "site_probe_enabled": (
                record.site_probe_enabled
                if record is not None
                else self._settings.site_probe_enabled
            )
        }

    def set_settings(self, *, site_probe_enabled: bool) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(SeedingHealthConfig, _CONFIG_ID)
            if record is None:
                record = SeedingHealthConfig(
                    id=_CONFIG_ID,
                    site_probe_enabled=site_probe_enabled,
                    updated_at=utc_now(),
                )
                session.add(record)
            else:
                record.site_probe_enabled = site_probe_enabled
                record.updated_at = utc_now()
            session.commit()
        return {"site_probe_enabled": site_probe_enabled}

    def _site_probe_enabled(self) -> bool:
        with self._session_factory() as session:
            record = session.get(SeedingHealthConfig, _CONFIG_ID)
        return (
            record.site_probe_enabled if record is not None else self._settings.site_probe_enabled
        )

    def list_policies(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(SeedingPolicy)).all()
        return [
            {
                "site_key": row.site_key,
                "min_seeding_hours": row.min_seeding_hours,
                "requires_hr": row.requires_hr,
                "source": row.source,
            }
            for row in rows
        ]

    def set_policy(self, site_key: str, *, min_seeding_hours: int | None) -> dict[str, Any]:
        if min_seeding_hours is not None and not 0 <= min_seeding_hours <= 8760:
            raise ApplicationError(
                code="SEEDING_POLICY_INVALID",
                status=422,
                title="站点 H&R 规则无效",
                detail="min_seeding_hours 必须位于 0..8760，或留空表示规则未知",
            )
        with self._session_factory() as session:
            record = session.get(SeedingPolicy, site_key)
            if record is None:
                record = SeedingPolicy(site_key=site_key, source="MANUAL", updated_at=utc_now())
                session.add(record)
            record.min_seeding_hours = min_seeding_hours
            record.source = "MANUAL"
            record.updated_at = utc_now()
            session.commit()
        return {
            "site_key": site_key,
            "min_seeding_hours": min_seeding_hours,
            "requires_hr": True,
            "source": "MANUAL",
        }

    # ------------------------------------------------------------------ 动作
    @staticmethod
    def _normalize_repair_action(action: str) -> str:
        normalized = action.strip().upper()
        if normalized not in {"RETRIEVE_TORRENT", "RESEED"}:
            raise ApplicationError(
                code="SEEDING_HEALTH_ACTION_UNKNOWN",
                status=422,
                title="未知的修复动作",
                detail=f"不支持的修复动作：{action}",
            )
        return normalized

    def _repair_context(
        self,
        issue_id: str,
        operation: str,
        downloader_id: str | None,
        save_path: str | None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            issue = session.get(SeedingHealthIssue, issue_id)
            watch = session.get(SeedingWatch, issue.watch_id) if issue else None
            if issue is None or watch is None:
                raise _not_found()
            if issue.status not in {item.value for item in ACTIVE_ISSUE_STATUSES}:
                raise ApplicationError(
                    code="SEEDING_HEALTH_ISSUE_NOT_ACTIONABLE",
                    status=409,
                    title="该异常当前不可修复",
                    detail=f"异常状态为 {issue.status}，请先处理当前状态",
                )
            if (
                issue.kind != SeedingIssueKind.SITE_MISSING.value
                and operation == "RETRIEVE_TORRENT"
            ):
                raise ApplicationError(
                    code="SEEDING_HEALTH_REPAIR_KIND_MISMATCH",
                    status=409,
                    title="动作与异常类型不匹配",
                    detail="重新拉取种子只适用于「站侧无记录」异常",
                )
            if issue.kind != SeedingIssueKind.SITE_ONLY.value and operation == "RESEED":
                raise ApplicationError(
                    code="SEEDING_HEALTH_REPAIR_KIND_MISMATCH",
                    status=409,
                    title="动作与异常类型不匹配",
                    detail="重建辅种只适用于「站侧独有」异常",
                )
            if not watch.site_key or not watch.remote_torrent_id:
                raise ApplicationError(
                    code="SEEDING_HEALTH_IDENTITY_REQUIRED",
                    status=409,
                    title="无法确定种子身份",
                    detail="该异常缺少站点种子 ID，不能从站点重新拉取",
                )
            detect_count = issue.detect_count
            site_key = watch.site_key.lower()
            remote_torrent_id = watch.remote_torrent_id
            watch_downloader_id = watch.downloader_id
            watch_save_path = watch.save_path
            watch_name = watch.torrent_name

        downloaders = [item for item in self._downloaders.list_downloaders() if item.enabled]
        chosen_id = downloader_id or watch_downloader_id
        target = next((item for item in downloaders if item.id == chosen_id), None)
        if target is None and downloader_id is None and len(downloaders) == 1:
            target = downloaders[0]
        if target is None:
            raise ApplicationError(
                code="SEEDING_HEALTH_DOWNLOADER_REQUIRED",
                status=422,
                title="请选择目标下载器",
                detail="当前异常没有绑定下载器，请显式指定 downloader_id",
            )
        resolved_save_path = save_path or (watch_save_path if watch_downloader_id else None)
        if not resolved_save_path:
            raise ApplicationError(
                code="SEEDING_HEALTH_SAVE_PATH_REQUIRED",
                status=422,
                title="请指定保存路径",
                detail="重建辅种无法从原任务推断保存路径，请显式指定 downloader 侧的 save_path",
            )
        return {
            "issue_id": issue_id,
            "operation": operation,
            "site_key": site_key,
            "remote_torrent_id": remote_torrent_id,
            "watch_name": watch_name,
            "detect_count": detect_count,
            "downloader_id": target.id,
            "downloader_name": target.name,
            "save_path": resolved_save_path,
        }

    async def _repair_preview_payload(self, context: dict[str, Any]) -> tuple[bytes, Any, Any]:
        binding = next(
            (
                item
                for item in self._sites.enabled_adapters()
                if item.site_id.lower() == context["site_key"]
            ),
            None,
        )
        if binding is None:
            raise ApplicationError(
                code="SEEDING_HEALTH_SITE_UNAVAILABLE",
                status=409,
                title="站点适配器不可用",
                detail="该站点当前未启用或没有可用适配器，无法重新拉取种子",
            )
        try:
            payload = await binding.adapter.fetch_torrent(context["remote_torrent_id"])
        except Exception as exc:
            raise ApplicationError(
                code="SEEDING_HEALTH_TORRENT_FETCH_FAILED",
                status=502,
                title="站点拉取种子失败",
                detail=f"{type(exc).__name__}: {exc}"[:500],
            ) from exc
        meta = parse_torrent(payload.content)
        digest = payload.sha256_digest or sha256(payload.content).hexdigest()
        return payload.content, digest, meta

    async def preview_repair(
        self,
        issue_id: str,
        action: str,
        *,
        downloader_id: str | None = None,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        operation = self._normalize_repair_action(action)
        context = self._repair_context(issue_id, operation, downloader_id, save_path)
        content, digest, meta = await self._repair_preview_payload(context)
        return {
            **context,
            "torrent_name": meta.display_name,
            "size_bytes": sum(item.length for item in meta.files),
            "file_count": len(meta.files),
            "v1_info_hash": meta.v1_info_hash,
            "v2_info_hash": meta.v2_info_hash,
            "metainfo_digest": meta.metainfo_digest,
            "payload_sha256": digest,
            "torrent_bytes": len(content),
            "warning": (
                "将以暂停状态加入下载器并执行完整内容校验；确认文件无误后需手动启动做种。"
                "校验完成前不要删除任何现有文件。"
            ),
        }

    @staticmethod
    async def _observe_torrent(adapter: Any, hashes: tuple[str, ...]) -> tuple[Any, ...]:
        """下载器加种后回读有最终一致性，有限重试后才判定未命中。"""
        for _ in range(3):
            try:
                observed = await adapter.get_torrents(hashes)
                if observed:
                    return tuple(observed)
            except Exception:  # noqa: BLE001 - 瞬时 RPC 失败继续重试
                pass
            await asyncio.sleep(0.3)
        return ()

    async def execute_repair(
        self,
        issue_id: str,
        action: str,
        *,
        expected_payload_sha256: str,
        downloader_id: str | None = None,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        operation = self._normalize_repair_action(action)
        context = self._repair_context(issue_id, operation, downloader_id, save_path)
        content, digest, meta = await self._repair_preview_payload(context)
        if digest != expected_payload_sha256:
            raise ApplicationError(
                code="SEEDING_HEALTH_REPAIR_DIGEST_MISMATCH",
                status=409,
                title="种子内容已变化",
                detail="预览之后站点种子发生变化，请重新预览再确认",
            )
        idempotency_key = f"{issue_id}:{operation}:{digest}:{context['detect_count']}"
        intent = {
            "expected_payload_sha256": digest,
            "metainfo_digest": meta.metainfo_digest,
            "v1_info_hash": meta.v1_info_hash,
            "v2_info_hash": meta.v2_info_hash,
        }
        target = {
            "downloader_id": context["downloader_id"],
            "save_path": context["save_path"],
        }
        with self._session_factory() as session:
            begin_immediate_write(session)
            existing = session.scalar(
                select(SeedingRepairJournal).where(
                    SeedingRepairJournal.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.status == OperationStatus.APPLIED.value:
                    return {
                        "journal_id": existing.id,
                        "status": existing.status,
                        "already_recorded": True,
                    }
                prior_intent = json.loads(existing.intent)
                prior_target = json.loads(existing.target)
                hashes = tuple(
                    value
                    for key in ("v1_info_hash", "v2_info_hash")
                    if (value := prior_intent.get(key))
                )
                if hashes:
                    try:
                        prior_binding = self._downloaders.write_binding(
                            prior_target["downloader_id"]
                        )
                        if await self._observe_torrent(prior_binding.adapter, hashes):
                            with self._session_factory() as recovery_session:
                                row = recovery_session.get(SeedingRepairJournal, existing.id)
                                if row is not None:
                                    row.status = OperationStatus.APPLIED.value
                                    row.updated_at = utc_now()
                                    recovery_session.commit()
                            return {
                                "journal_id": existing.id,
                                "status": OperationStatus.APPLIED.value,
                                "already_recorded": True,
                            }
                    except Exception:  # noqa: BLE001 - 恢复失败则保留原状态
                        pass
                return {
                    "journal_id": existing.id,
                    "status": existing.status,
                    "already_recorded": True,
                }
            journal_row = SeedingRepairJournal(
                id=new_uuid(),
                issue_id=issue_id,
                idempotency_key=idempotency_key,
                operation_type=operation,
                target=json.dumps(target, ensure_ascii=False),
                intent=json.dumps(intent, ensure_ascii=False),
                status=OperationStatus.INTENT_RECORDED.value,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(journal_row)
            session.commit()
            journal_id = journal_row.id

        status = OperationStatus.RECONCILE_REQUIRED.value
        error_code: str | None = None
        added_hash: str | None = None
        try:
            binding = self._downloaders.write_binding(context["downloader_id"])
            adapter = binding.adapter
            if hasattr(adapter, "add_torrent") and hasattr(adapter, "get_torrents"):
                downloader = next(
                    item
                    for item in self._downloaders.list_downloaders()
                    if item.id == context["downloader_id"]
                )
                is_transmission = DownloaderKind(downloader.type) is DownloaderKind.TRANSMISSION
                if is_transmission:
                    result = await adapter.add_torrent(
                        TransmissionAddRequest(
                            torrent_content=content,
                            save_path=context["save_path"],
                            labels=("packbreaker-repair",),
                            paused=True,
                        )
                    )
                else:
                    result = await adapter.add_torrent(
                        QbittorrentAddRequest(
                            torrent_content=content,
                            save_path=context["save_path"],
                            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
                            skip_checking=False,
                            tags=("packbreaker-repair",),
                            paused=True,
                        )
                    )
                added_hash = result.torrent_hash
                observed = await self._observe_torrent(adapter, (added_hash,))
                status = (
                    OperationStatus.APPLIED.value
                    if observed
                    else OperationStatus.RECONCILE_REQUIRED.value
                )
            else:
                error_code = "SEEDING_HEALTH_WRITER_UNAVAILABLE"
        except Exception as exc:
            error_code = type(exc).__name__

        with self._session_factory() as session:
            journal = session.get(SeedingRepairJournal, journal_id)
            if journal is not None:
                journal.status = status
                journal.last_error_code = error_code
                journal.after_snapshot = json.dumps(
                    {"torrent_hash": added_hash, "observed": status}, ensure_ascii=False
                )
                journal.updated_at = utc_now()
            session.commit()

        return {
            "journal_id": journal_id,
            "status": status,
            "torrent_hash": added_hash,
            "already_recorded": False,
        }

    async def act(self, issue_id: str, action: str) -> dict[str, Any]:
        normalized = action.strip().upper()
        if normalized in {"ACK", "IGNORE"}:
            return self._set_status(issue_id, normalized)
        if normalized == "RECHECK":
            return await self._recheck(issue_id)
        if normalized in {"RETRIEVE_TORRENT", "RESEED"}:
            raise ApplicationError(
                code="SEEDING_HEALTH_ACTION_PREVIEW_REQUIRED",
                status=409,
                title="该动作需要先预览并确认",
                detail="请通过修复预览接口获取种子摘要与摘要值，确认后再提交执行",
            )
        raise ApplicationError(
            code="SEEDING_HEALTH_ACTION_UNKNOWN",
            status=422,
            title="未知的修复动作",
            detail=f"不支持的动作：{action}",
        )

    def _set_status(self, issue_id: str, action: str) -> dict[str, Any]:
        with self._session_factory() as session:
            issue = session.get(SeedingHealthIssue, issue_id)
            if issue is None:
                raise _not_found()
            issue.status = (
                SeedingIssueStatus.ACKED.value
                if action == "ACK"
                else SeedingIssueStatus.IGNORED.value
            )
            issue.updated_at = utc_now()
            session.commit()
            return {"id": issue.id, "status": issue.status}

    async def _recheck(self, issue_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            issue = session.get(SeedingHealthIssue, issue_id)
            watch = session.get(SeedingWatch, issue.watch_id) if issue is not None else None
            if issue is None or watch is None:
                raise _not_found()
            downloader_id = watch.downloader_id
            torrent_hash = watch.torrent_hash
        if downloader_id is None:
            raise ApplicationError(
                code="SEEDING_HEALTH_ACTION_MANUAL",
                status=409,
                title="该种子不在下载器中",
                detail="站侧独有的种子无法执行下载器重校验，请先确认是否需要重新加种",
            )
        binding = self._downloaders.write_binding(downloader_id)
        adapter = binding.adapter
        try:
            if hasattr(adapter, "recheck_torrent"):
                await adapter.recheck_torrent(torrent_hash)
            elif hasattr(adapter, "verify_torrent"):
                await adapter.verify_torrent(torrent_hash)
            else:
                raise ApplicationError(
                    code="SEEDING_HEALTH_ACTION_UNSUPPORTED",
                    status=409,
                    title="下载器不支持重校验",
                    detail="当前下载器未提供强制重校验能力",
                )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - 适配器异常统一收敛为稳定错误码
            raise ApplicationError(
                code="SEEDING_HEALTH_ACTION_FAILED",
                status=502,
                title="强制重校验失败",
                detail=str(exc),
            ) from exc
        with self._session_factory() as session:
            issue = session.get(SeedingHealthIssue, issue_id)
            if issue is None:
                raise _not_found()
            issue.status = SeedingIssueStatus.REPAIRING.value
            issue.updated_at = utc_now()
            session.commit()
            return {"id": issue.id, "status": issue.status}

    # ------------------------------------------------------------------ 巡查
    def start_run(self, trigger: SeedingRunTrigger = SeedingRunTrigger.SCHEDULED) -> str:
        """只创建 RUNNING 记录并返回 run_id，调用方随后异步执行巡查。"""
        run_id = new_uuid()
        with self._session_factory() as session:
            # 只收尾超过一轮超时的孤儿记录，绝不取消仍在执行的巡查。
            cutoff = utc_now() - timedelta(seconds=self._settings.run_timeout_seconds)
            for stale in session.scalars(
                select(SeedingHealthRun).where(
                    SeedingHealthRun.status == SeedingRunStatus.RUNNING.value,
                    SeedingHealthRun.started_at < cutoff,
                )
            ).all():
                stale.status = SeedingRunStatus.CANCELLED.value
                stale.finished_at = utc_now()
                stale.error_summary = "进程中断，本轮巡查未完成"
            session.add(
                SeedingHealthRun(
                    id=run_id,
                    trigger=trigger.value,
                    status=SeedingRunStatus.RUNNING.value,
                    started_at=utc_now(),
                )
            )
            session.commit()
        return run_id

    async def run_once(
        self,
        trigger: SeedingRunTrigger = SeedingRunTrigger.SCHEDULED,
        *,
        run_id: str | None = None,
    ) -> str:
        if run_id is None:
            run_id = self.start_run(trigger)

        try:
            async with asyncio.timeout(self._settings.run_timeout_seconds):
                counters = await self._patrol()
        except TimeoutError:
            # 已完成的站点结果已经落库，超时只降级为 partial，不回滚。
            self._finish_run(run_id, SeedingRunStatus.PARTIAL, error="巡查超时，已完成部分保留")
            return run_id
        except asyncio.CancelledError:
            self._finish_run(run_id, SeedingRunStatus.CANCELLED, error="巡查被取消")
            raise
        except Exception as exc:
            self._logger.exception("做种健康巡查失败 run_id=%s", run_id)
            self._finish_run(run_id, SeedingRunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            raise

        status = SeedingRunStatus.OK
        if counters["degraded"] or counters["downloader_errors"]:
            status = SeedingRunStatus.PARTIAL
        if counters["ok_downloaders"] == 0 and counters["downloader_errors"]:
            status = SeedingRunStatus.FAILED
        self._finish_run(
            run_id,
            status,
            scanned=counters["watches"],
            created=counters["created"],
            resolved=counters["resolved"],
            degraded=counters["degraded"],
            error="; ".join(counters["error_messages"]) or None,
        )
        return run_id

    def _finish_run(
        self,
        run_id: str,
        status: SeedingRunStatus,
        *,
        scanned: int = 0,
        created: int = 0,
        resolved: int = 0,
        degraded: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            run = session.get(SeedingHealthRun, run_id)
            if run is None:
                return
            run.status = status.value
            run.finished_at = utc_now()
            run.scanned_watches = scanned
            run.created_issues = created
            run.resolved_issues = resolved
            run.degraded_sites = json.dumps(degraded, ensure_ascii=False) if degraded else None
            run.error_summary = error[:2000] if error else None
            session.commit()

    async def _patrol(self) -> dict[str, Any]:
        counters: dict[str, Any] = {
            "watches": 0,
            "created": 0,
            "resolved": 0,
            "degraded": [],
            "downloader_errors": 0,
            "ok_downloaders": 0,
            "error_messages": [],
            "site_only": 0,
        }
        downloaders = [item for item in self._downloaders.list_downloaders() if item.enabled]
        collected: dict[str, tuple[Any, ...]] = {}
        for downloader in downloaders:
            try:
                collected[downloader.id] = await self._downloaders.list_all_torrents(downloader.id)
                counters["ok_downloaders"] += 1
            except ApplicationError as exc:
                counters["downloader_errors"] += 1
                counters["error_messages"].append(f"{downloader.name}: {exc.code}")

        if not collected:
            return counters

        gone_watch_ids = await asyncio.to_thread(self._sync_watches, collected)
        counters["resolved"] += await asyncio.to_thread(self._resolve_gone, gone_watch_ids)

        targets = await asyncio.to_thread(self._load_targets)
        counters["watches"] = len(targets)

        degraded = await self._probe_sites(targets)
        counters["degraded"] = degraded

        files = await asyncio.to_thread(self._check_files, targets)
        tracker_states = await self._sample_trackers(targets)

        created, resolved = await asyncio.to_thread(
            self._apply_findings, targets, files, tracker_states, set(degraded)
        )
        counters["created"] = created
        counters["resolved"] += resolved
        await self._detect_site_only(counters)
        return counters

    # -------------------------------------------------------------- 数据同步
    def _sync_watches(self, collected: dict[str, tuple[Any, ...]]) -> list[str]:
        now = utc_now()
        gone_watch_ids: list[str] = []
        with self._session_factory() as session:
            for downloader_id, torrents in collected.items():
                existing = {
                    item.torrent_hash: item
                    for item in session.scalars(
                        select(SeedingWatch).where(SeedingWatch.downloader_id == downloader_id)
                    )
                }
                present: set[str] = set()
                for torrent in torrents:
                    if float(torrent.progress) < _SEEDING_TOLERANCE:
                        continue
                    present.add(torrent.torrent_hash)
                    watch = existing.get(torrent.torrent_hash)
                    identity = identity_from_comment(
                        torrent.comment if isinstance(torrent.comment, str) else None
                    )
                    if watch is None:
                        watch = SeedingWatch(
                            id=new_uuid(),
                            downloader_id=downloader_id,
                            torrent_hash=torrent.torrent_hash,
                            torrent_name=torrent.name[:1024],
                            size_bytes=int(torrent.size_bytes),
                            save_path=torrent.save_path[:2048],
                            content_path=torrent.content_path or None,
                            site_key=identity.site_key if identity else None,
                            remote_torrent_id=identity.torrent_id if identity else None,
                            identity_source=identity.source.value if identity else None,
                            presence=SeedingWatchPresence.ACTIVE.value,
                            first_seen_at=now,
                            last_seen_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(watch)
                    else:
                        watch.presence = SeedingWatchPresence.ACTIVE.value
                        watch.last_seen_at = now
                        watch.torrent_name = torrent.name[:1024]
                        watch.size_bytes = int(torrent.size_bytes)
                        watch.save_path = torrent.save_path[:2048]
                        watch.content_path = torrent.content_path or None
                        watch.updated_at = now
                        if not watch.identity_locked and identity is not None:
                            watch.site_key = identity.site_key
                            watch.remote_torrent_id = identity.torrent_id
                            watch.identity_source = identity.source.value
                    watch.seeding_time_seconds = torrent.seeding_time_seconds
                    watch.tracker_state = torrent.tracker_state
                    watch.tracker_message = (torrent.tracker_message or "")[:512] or None
                for torrent_hash, watch in existing.items():
                    if (
                        torrent_hash not in present
                        and watch.presence != SeedingWatchPresence.GONE.value
                    ):
                        watch.presence = SeedingWatchPresence.GONE.value
                        watch.updated_at = now
                        gone_watch_ids.append(watch.id)
            session.commit()
        return gone_watch_ids

    def _resolve_gone(self, watch_ids: list[str]) -> int:
        if not watch_ids:
            return 0
        resolved = 0
        with self._session_factory() as session:
            issues = session.scalars(
                select(SeedingHealthIssue).where(
                    SeedingHealthIssue.watch_id.in_(watch_ids),
                    SeedingHealthIssue.status.in_([item.value for item in ACTIVE_ISSUE_STATUSES]),
                )
            ).all()
            for issue in issues:
                issue.status = SeedingIssueStatus.RESOLVED.value
                issue.resolved_at = utc_now()
                issue.resolved_reason = SeedingIssueResolution.GONE.value
                issue.updated_at = utc_now()
                resolved += 1
            session.commit()
        return resolved

    def _load_targets(self) -> list[_WatchTarget]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SeedingWatch)
                .where(SeedingWatch.presence == SeedingWatchPresence.ACTIVE.value)
                .order_by(SeedingWatch.last_seen_at.desc())
                .limit(self._settings.max_watches)
            ).all()
            return [_target_of(row) for row in rows]

    # -------------------------------------------------------------- 站点探测
    async def _probe_sites(self, targets: list[_WatchTarget]) -> list[str]:
        if not self._site_probe_enabled():
            return []
        sites = {item.site_id: item for item in self._sites.enabled_adapters()}
        ttl = timedelta(hours=self._settings.detail_ttl_hours)
        now = utc_now()
        grouped: dict[str, list[_WatchTarget]] = {}
        for target in targets:
            if not target.site_key or not target.remote_torrent_id:
                continue
            if target.site_key not in sites:
                continue
            # 不能依赖 tracker 报错做门槛：Transmission 4.1.3 不返回
            # trackerStats，会把站点侧检查整体关掉。改为 TTL 抽样轮转，
            # 预算见 _site_budget()。
            if target.checked_at is not None and now - target.checked_at < ttl:
                continue
            grouped.setdefault(target.site_key, []).append(target)

        # 最久未检查的先探测，保证大库在预算内轮转覆盖而不是永远查前 N 个。
        for site_key, batch in grouped.items():
            batch.sort(key=lambda item: item.checked_at or now)
            grouped[site_key] = batch[: max(1, self._settings.site_probe_budget)]
        if not grouped:
            return []

        degraded: list[str] = []
        for site_key, batch in grouped.items():
            binding = sites[site_key]
            # 站点自己的请求间隔才是真瓶颈：M-Team 90 秒间隔下，120 个探测
            # 要 3 小时，远超单轮预算。
            batch = batch[: self._site_budget(binding)]
            semaphore = asyncio.Semaphore(_SITE_CONCURRENCY)

            async def probe(
                target: _WatchTarget, gate: asyncio.Semaphore = semaphore, b: Any = binding
            ) -> tuple[str, SiteProbeOutcome]:
                async with gate:
                    return target.watch_id, await _probe_one(b.adapter, target)

            results = await asyncio.gather(*(probe(target) for target in batch))
            missing = sum(1 for _, outcome in results if outcome is SiteProbeOutcome.MISSING)
            tally = SiteProbeTally(
                site_key=site_key,
                probed=len(results),
                missing=missing,
                unknown=sum(1 for _, outcome in results if outcome is SiteProbeOutcome.UNKNOWN),
            )
            breached = is_site_degraded(tally, ratio=self._settings.degrade_ratio)
            if breached:
                degraded.append(site_key)
            outcomes: dict[str, SiteProbeOutcome] = {}
            for watch_id, outcome in results:
                outcomes[watch_id] = (
                    SiteProbeOutcome.UNKNOWN
                    if breached and outcome is SiteProbeOutcome.MISSING
                    else outcome
                )
            self._logger.info(
                "站点探测完成 site=%s probed=%s missing=%s degraded=%s",
                site_key,
                len(results),
                missing,
                breached,
            )
            # 每站点完成即落库：单轮超时后下一轮能从断点继续。
            await asyncio.to_thread(self._store_outcomes, outcomes, now)
        return degraded

    async def _detect_site_only(self, counters: dict[str, int]) -> None:
        """站点做种列表 − 本地做种库存 = 站侧独有种子。

        只有把某站的做种列表**完整取回**才做差集；列表未取全、站点返回不支持、
        或 Cookie 失效时一律跳过，宁可漏报也不把整站误判成「站侧有、本地无」。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(SeedingWatch.site_key, SeedingWatch.remote_torrent_id).where(
                    SeedingWatch.presence == SeedingWatchPresence.ACTIVE.value,
                    SeedingWatch.site_key.is_not(None),
                    SeedingWatch.remote_torrent_id.is_not(None),
                )
            ).all()
        local_by_site: dict[str, set[str]] = {}
        for site_key, remote_id in rows:
            local_by_site.setdefault(site_key, set()).add(remote_id)

        now = utc_now()
        for binding in self._sites.enabled_adapters():
            list_seeding = getattr(binding.adapter, "list_seeding", None)
            if list_seeding is None:
                continue
            site_key = binding.site_id.lower()
            seen: set[str] = set()
            titles: dict[str, str] = {}
            cursor: str | None = None
            complete = False
            for _ in range(_MAX_SITE_ONLY_PAGES):
                try:
                    page = await list_seeding(cursor)
                except Exception as exc:  # noqa: BLE001 - 站点异常一律跳过该站
                    self._logger.warning(
                        "站点做种列表读取失败，跳过 SITE_ONLY site=%s error=%s",
                        site_key,
                        type(exc).__name__,
                    )
                    break
                if not page.supported:
                    break
                for item in page.items:
                    seen.add(item.torrent_id)
                    titles[item.torrent_id] = item.title
                if page.total_hint is not None and len(seen) >= page.total_hint:
                    complete = True
                    break
                if page.next_cursor is None:
                    complete = True
                    break
                cursor = page.next_cursor
            if not complete:
                self._logger.warning(
                    "站点做种列表未取全，跳过 SITE_ONLY site=%s seen=%s",
                    site_key,
                    len(seen),
                )
                continue
            local_ids = local_by_site.get(site_key, set())
            if not local_ids or len(local_ids) < len(seen) * _SITE_ONLY_MIN_COVERAGE:
                self._logger.warning(
                    "本地该站身份覆盖不足，跳过 SITE_ONLY site=%s local=%s site_total=%s",
                    site_key,
                    len(local_ids),
                    len(seen),
                )
                continue
            missing = seen - local_ids
            self._logger.info(
                "站点独有种子 site=%s count=%s total=%s", site_key, len(missing), len(seen)
            )
            counters["site_only"] += await asyncio.to_thread(
                self._store_site_only, site_key, missing, seen, local_ids, titles, now
            )

    def _store_site_only(
        self,
        site_key: str,
        missing: set[str],
        site_seen: set[str],
        local_ids: set[str],
        titles: dict[str, str],
        now: datetime,
    ) -> int:
        score = risk_score(SeedingIssueKind.SITE_ONLY)
        created = 0
        with self._session_factory() as session:
            # 站点已无该种子，或它已被加回本地下载器时，SITE_ONLY 不再成立。
            stale = session.scalars(
                select(SeedingWatch).where(
                    SeedingWatch.presence == SeedingWatchPresence.SITE_ONLY.value,
                    SeedingWatch.site_key == site_key,
                )
            ).all()
            for old_watch in stale:
                remote_id = old_watch.remote_torrent_id
                if remote_id in site_seen and remote_id not in local_ids:
                    # 站点仍有、本地仍无：SITE_ONLY 判定继续成立。
                    continue
                active_issue = session.scalars(
                    select(SeedingHealthIssue).where(
                        SeedingHealthIssue.watch_id == old_watch.id,
                        SeedingHealthIssue.kind == SeedingIssueKind.SITE_ONLY.value,
                        SeedingHealthIssue.status.in_(
                            [item.value for item in ACTIVE_ISSUE_STATUSES]
                        ),
                    )
                ).first()
                if active_issue is not None:
                    active_issue.status = SeedingIssueStatus.RESOLVED.value
                    active_issue.resolved_reason = "GONE"
                    active_issue.resolved_at = now
                    active_issue.updated_at = now
            for torrent_id in sorted(missing):
                watch_id = str(
                    uuid5(NAMESPACE_URL, f"packbreaker:site-only:{site_key}:{torrent_id}")
                )
                watch = session.get(SeedingWatch, watch_id)
                if watch is None:
                    watch = SeedingWatch(
                        id=watch_id,
                        downloader_id=None,
                        torrent_hash=f"site:{site_key}:{torrent_id}"[:64],
                        torrent_name=titles.get(torrent_id, torrent_id)[:1024],
                        size_bytes=0,
                        save_path="",
                        site_key=site_key,
                        remote_torrent_id=torrent_id,
                        presence=SeedingWatchPresence.SITE_ONLY.value,
                        first_seen_at=now,
                        last_seen_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(watch)
                    # issue.watch_id 有外键，必须先让虚拟 watch 落库。
                    session.flush()
                else:
                    watch.last_seen_at = now
                    watch.updated_at = now
                evidence = json.dumps(
                    {
                        "site": {
                            "result": "SITE_ONLY",
                            "consecutive_misses": 0,
                            "detail": "站点做种列表存在，下载器中没有对应任务",
                        },
                        "tracker": {"state": "UNKNOWN", "message": None},
                        "filesystem": {"path": None, "exists": None},
                        "summary": (f"站点种子 ID {torrent_id}，本地下载器未找到对应任务"),
                        "site_torrent_id": torrent_id,
                    },
                    ensure_ascii=False,
                )
                if _upsert_issue(
                    session,
                    watch_id=watch_id,
                    kind=SeedingIssueKind.SITE_ONLY,
                    score=score,
                    remaining=None,
                    evidence=evidence,
                    now=now,
                ):
                    created += 1
            session.commit()
        return created

    def _site_budget(self, binding: Any) -> int:
        base = max(1, self._settings.site_probe_budget)
        interval = float(getattr(binding, "search_interval_seconds", 0) or 0)
        if interval >= 60:
            # M-Team 单请求 90 秒，单轮最多给 6 分钟。
            return min(base, 4)
        if interval >= 20:
            return min(base, 10)
        if interval >= 5:
            return min(base, 15)
        return base

    def _store_outcomes(self, outcomes: dict[str, SiteProbeOutcome], checked_at: datetime) -> None:
        with self._session_factory() as session:
            for watch_id, outcome in outcomes.items():
                watch = session.get(SeedingWatch, watch_id)
                if watch is None:
                    continue
                watch.site_checked_at = checked_at
                watch.site_outcome = outcome.value
                if outcome is SiteProbeOutcome.MISSING:
                    watch.consecutive_misses += 1
                elif outcome is SiteProbeOutcome.FOUND:
                    watch.consecutive_misses = 0
                watch.updated_at = checked_at
            session.commit()

    # -------------------------------------------------------------- 文件检查
    def _check_files(self, targets: list[_WatchTarget]) -> dict[str, bool | None]:
        rules: dict[str, list[PathMappingRule]] = {}
        for downloader in self._downloaders.list_downloaders():
            rules[downloader.id] = list(downloader.path_mappings)
        result: dict[str, bool | None] = {}
        for target in targets:
            raw_path = target.content_path or target.save_path
            matched = _map_path(raw_path, rules.get(target.downloader_id, []))
            if matched is None:
                result[target.watch_id] = None
                continue
            container_path, mount_root = matched
            result[target.watch_id] = _path_visible(container_path, mount_root)
        return result

    # -------------------------------------------------------------- Tracker
    async def _sample_trackers(
        self, targets: list[_WatchTarget]
    ) -> dict[str, tuple[str, str | None]]:
        ttl = timedelta(hours=self._settings.detail_ttl_hours)
        now = utc_now()
        candidates = [
            target
            for target in targets
            if target.site_key is not None
            and (target.tracker_checked_at is None or now - target.tracker_checked_at > ttl)
        ]
        if not candidates:
            return {}
        # 先补从未采样的种子，再轮转已过期的，保证每轮都有推进。
        epoch = datetime.min.replace(tzinfo=UTC)
        candidates.sort(key=lambda item: item.tracker_checked_at or epoch)
        need = candidates[: self._settings.tracker_batch]
        grouped: dict[str, list[_WatchTarget]] = {}
        for target in need:
            grouped.setdefault(target.downloader_id, []).append(target)

        result: dict[str, tuple[str, str | None]] = {}
        for downloader_id, batch in grouped.items():
            try:
                binding = self._downloaders.write_binding(downloader_id)
            except ApplicationError:
                continue
            adapter = binding.adapter
            if not hasattr(adapter, "get_tracker_states"):
                continue
            try:
                states = await adapter.get_tracker_states(
                    tuple(item.torrent_hash for item in batch)
                )
            except Exception as exc:  # noqa: BLE001 - 采样失败不影响其他判定
                self._logger.debug(
                    "tracker 采样失败 downloader=%s error=%s",
                    downloader_id,
                    type(exc).__name__,
                )
                continue
            for target in batch:
                state = states.get(target.torrent_hash)
                if state is not None:
                    result[target.watch_id] = (state.state, state.message)
        if result:
            await asyncio.to_thread(self._store_tracker_states, result)
        return result

    def _store_tracker_states(self, states: dict[str, tuple[str, str | None]]) -> None:
        now = utc_now()
        with self._session_factory() as session:
            for watch_id, (state, message) in states.items():
                watch = session.get(SeedingWatch, watch_id)
                if watch is None:
                    continue
                watch.tracker_state = state
                watch.tracker_message = (message or "")[:512] or None
                watch.tracker_checked_at = now
                watch.updated_at = now
            session.commit()

    # -------------------------------------------------------------- 落库
    def _apply_findings(
        self,
        targets: list[_WatchTarget],
        files: dict[str, bool | None],
        tracker_states: dict[str, tuple[str, str | None]],
        degraded_sites: set[str],
    ) -> tuple[int, int]:
        now = utc_now()
        created = 0
        resolved = 0
        policies = self._load_policies()
        file_representatives: dict[str, str] = {}
        file_affected: dict[str, int] = {}
        for target in targets:
            if files.get(target.watch_id) is not False:
                continue
            path = target.content_path or target.save_path
            file_representatives.setdefault(path, target.watch_id)
            file_affected[path] = file_affected.get(path, 0) + 1
        with self._session_factory() as session:
            for target in targets:
                if target.site_key in degraded_sites:
                    continue
                # 探测结果与连续计数是上一阶段刚写库的，必须重读，否则本轮的
                # 判定会用上一轮的旧值，阈值永远差一次。
                watch = session.get(SeedingWatch, target.watch_id)
                if watch is None:
                    continue
                target = replace(
                    target,
                    site_outcome=_outcome_of_value(watch.site_outcome),
                    consecutive_misses=watch.consecutive_misses,
                )
                remaining = hnr_remaining_hours(
                    required_hours=policies.get(target.site_key or ""),
                    seeding_seconds=target.seeding_time_seconds,
                )
                tracker_state, tracker_message = tracker_states.get(
                    target.watch_id, (target.tracker_state, target.tracker_message)
                )
                visible = files.get(target.watch_id)

                desired: dict[SeedingIssueKind, dict[str, Any]] = {}
                if (
                    target.site_key
                    and target.site_outcome is SiteProbeOutcome.MISSING
                    and should_confirm_missing(
                        target.site_outcome,
                        target.consecutive_misses,
                        self._settings.confirm_threshold,
                    )
                ):
                    desired[SeedingIssueKind.SITE_MISSING] = {
                        "detail": (
                            f"站点详情页返回 404，连续 {target.consecutive_misses} 次巡查无记录"
                        ),
                        "site": {
                            "result": SiteProbeOutcome.MISSING.value,
                            "consecutive_misses": target.consecutive_misses,
                        },
                    }
                if tracker_state == "ERROR":
                    desired[SeedingIssueKind.TRACKER_ERROR] = {
                        "detail": tracker_message or "tracker 最近一次通告失败",
                        "tracker": {"state": "ERROR", "message": tracker_message},
                    }
                if visible is False:
                    # 一个目录缺失往往影响多个同片种子，只按路径报一次；
                    # 否则 13 个种子会产生 13 条一模一样的异常。
                    path = target.content_path or target.save_path
                    if file_representatives.get(path) == target.watch_id:
                        affected = file_affected.get(path, 1)
                        desired[SeedingIssueKind.FILE_MISSING] = {
                            "detail": (
                                "种子内容路径在容器内不存在"
                                if affected <= 1
                                else f"种子内容路径在容器内不存在，影响 {affected} 个种子"
                            ),
                            "filesystem": {
                                "path": path,
                                "exists": False,
                                "affected_torrents": affected,
                            },
                        }
                    elif _clear_issue(session, target.watch_id, SeedingIssueKind.FILE_MISSING, now):
                        # 同路径的非代表种子：清掉历史遗留的重复异常。
                        resolved += 1

                for kind, payload in desired.items():
                    evidence = _build_evidence(
                        target=target,
                        payload=payload,
                        tracker_state=tracker_state,
                        tracker_message=tracker_message,
                        files_visible=visible,
                    )
                    score = risk_score(
                        kind,
                        hnr_remaining=remaining,
                        persistent_days=_persistence_days(target, now),
                    )
                    if _upsert_issue(
                        session,
                        watch_id=target.watch_id,
                        kind=kind,
                        score=score,
                        remaining=remaining,
                        evidence=evidence,
                        now=now,
                    ):
                        created += 1

                for kind in (SeedingIssueKind.SITE_MISSING, SeedingIssueKind.FILE_MISSING):
                    if kind in desired:
                        continue
                    observed = (
                        target.site_outcome if kind is SeedingIssueKind.SITE_MISSING else visible
                    )
                    if kind is SeedingIssueKind.SITE_MISSING:
                        if observed is not SiteProbeOutcome.FOUND:
                            continue
                    elif observed is not True:
                        continue
                    if _clear_issue(session, target.watch_id, kind, now):
                        resolved += 1
                if (
                    SeedingIssueKind.TRACKER_ERROR not in desired
                    and tracker_state == "OK"
                    and _clear_issue(session, target.watch_id, SeedingIssueKind.TRACKER_ERROR, now)
                ):
                    resolved += 1
            session.commit()
        return created, resolved

    def _load_policies(self) -> dict[str, int | None]:
        with self._session_factory() as session:
            rows = session.scalars(select(SeedingPolicy)).all()
            return {row.site_key: row.min_seeding_hours for row in rows}


# --------------------------------------------------------------------- helpers
def _not_found() -> ApplicationError:
    return ApplicationError(
        code="SEEDING_HEALTH_NOT_FOUND",
        status=404,
        title="记录不存在",
        detail="指定的做种健康记录不存在或已删除",
    )


def _run_view(run: SeedingHealthRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "scanned_watches": run.scanned_watches,
        "created_issues": run.created_issues,
        "resolved_issues": run.resolved_issues,
        "degraded_sites": json.loads(run.degraded_sites) if run.degraded_sites else [],
        "error_summary": run.error_summary,
    }


def _is_stale(run: SeedingHealthRun | None) -> bool:
    if run is None:
        return True
    if run.status == SeedingRunStatus.RUNNING.value:
        # 正在巡查时数据是新鲜的，不能提示「可能已过期」。
        return False
    if run.finished_at is None:
        return True
    return datetime.now(UTC) - run.finished_at > timedelta(hours=_STALE_AFTER_HOURS)


def _issue_row(issue: SeedingHealthIssue, watch: SeedingWatch) -> dict[str, Any]:
    return {
        "id": issue.id,
        "kind": issue.kind,
        "kind_label": _ISSUE_LABEL.get(issue.kind, issue.kind),
        "severity": issue.severity,
        "risk_score": issue.risk_score,
        "status": issue.status,
        "suggestion": issue.suggestion,
        "detect_count": issue.detect_count,
        "first_detected_at": issue.first_detected_at.isoformat(),
        "last_detected_at": issue.last_detected_at.isoformat(),
        "torrent_name": watch.torrent_name,
        "torrent_hash": watch.torrent_hash,
        "site_key": watch.site_key,
        "downloader_id": watch.downloader_id,
        "size_bytes": watch.size_bytes,
        "consecutive_misses": watch.consecutive_misses,
        "affected_torrents": _affected_torrents(issue.evidence),
    }


def _affected_torrents(evidence: str) -> int:
    try:
        payload = json.loads(evidence)
    except ValueError:
        return 1
    filesystem = payload.get("filesystem")
    if isinstance(filesystem, dict):
        value = filesystem.get("affected_torrents")
        if isinstance(value, int) and value > 0:
            return value
    return 1


def _issue_detail(issue: SeedingHealthIssue, watch: SeedingWatch) -> dict[str, Any]:
    try:
        evidence = json.loads(issue.evidence)
    except ValueError:
        evidence = {}
    return {
        **_issue_row(issue, watch),
        "evidence": evidence,
        "hnr_remaining_hours": issue.hnr_remaining_hours,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "resolved_reason": issue.resolved_reason,
        "save_path": watch.save_path,
        "content_path": watch.content_path,
        "identity_source": watch.identity_source,
        "identity_locked": watch.identity_locked,
        "seeding_time_seconds": watch.seeding_time_seconds,
    }


def _target_of(row: SeedingWatch) -> _WatchTarget:
    try:
        outcome = (
            SiteProbeOutcome(row.site_outcome) if row.site_outcome else SiteProbeOutcome.UNKNOWN
        )
    except ValueError:
        outcome = SiteProbeOutcome.UNKNOWN
    return _WatchTarget(
        watch_id=row.id,
        downloader_id=row.downloader_id or "",
        torrent_hash=row.torrent_hash,
        torrent_name=row.torrent_name,
        size_bytes=row.size_bytes,
        site_key=row.site_key,
        remote_torrent_id=row.remote_torrent_id,
        site_outcome=outcome,
        checked_at=row.site_checked_at,
        consecutive_misses=row.consecutive_misses,
        content_path=row.content_path,
        save_path=row.save_path,
        seeding_time_seconds=row.seeding_time_seconds,
        tracker_state=row.tracker_state,
        tracker_message=row.tracker_message,
        tracker_checked_at=row.tracker_checked_at,
    )


async def _probe_one(adapter: Any, target: _WatchTarget) -> SiteProbeOutcome:
    """只把明确的 404 当作缺失；其余错误一律未知。"""
    if not target.remote_torrent_id:
        return SiteProbeOutcome.UNKNOWN
    try:
        await adapter.fetch_details(target.remote_torrent_id)
    except Exception as exc:  # noqa: BLE001 - 需要按适配器错误码分类
        return (
            SiteProbeOutcome.MISSING
            if getattr(exc, "code", "") == "SITE_NOT_FOUND"
            else (SiteProbeOutcome.UNKNOWN)
        )
    return SiteProbeOutcome.FOUND


def _map_path(raw_path: str, rules: list[PathMappingRule]) -> tuple[Path, Path] | None:
    """返回（容器路径, 该映射的挂载根）。"""
    if not rules:
        return None
    try:
        match = map_remote_path(raw_path, rules)
    except Exception:  # noqa: BLE001 - 映射缺失等于路径不可见
        return None
    mount_root = Path(rules[match.rule_index].container_prefix)
    return match.container_path, mount_root


def _outcome_of_value(value: str | None) -> SiteProbeOutcome:
    if not value:
        return SiteProbeOutcome.UNKNOWN
    try:
        return SiteProbeOutcome(value)
    except ValueError:
        return SiteProbeOutcome.UNKNOWN


def _path_visible(path: Path, mount_root: Path) -> bool | None:
    """以挂载根作为「能否判断」的锚点。

    挂载点可达而路径不存在 → 真实缺失（包括整个子目录被删的情况）；
    挂载点本身不可见（盘没挂上）→ UNKNOWN，绝不误报。
    """
    try:
        if path.exists():
            return True
        if not mount_root.exists():
            return None
    except OSError:
        return None
    return False


def _persistence_days(target: _WatchTarget, now: datetime) -> float:
    if target.checked_at is None:
        return 0.0
    return max(0.0, (now - target.checked_at).total_seconds() / 86400.0)


def _build_evidence(
    *,
    target: _WatchTarget,
    payload: dict[str, Any],
    tracker_state: str | None,
    tracker_message: str | None,
    files_visible: bool | None,
) -> str:
    site = payload.get("site") or {
        "result": target.site_outcome.value,
        "consecutive_misses": target.consecutive_misses,
        "detail": (
            "本次未探测站点侧记录"
            if target.site_outcome is SiteProbeOutcome.UNKNOWN
            else f"站点侧状态：{target.site_outcome.value}"
        ),
    }
    tracker = payload.get("tracker") or {
        "state": tracker_state or "UNKNOWN",
        "message": tracker_message,
    }
    filesystem = payload.get("filesystem") or {
        "path": target.content_path or target.save_path,
        "exists": files_visible,
    }
    evidence = {
        "site": site,
        "downloader": {"state": "seeding", "detail": "下载器内任务存在且已完成下载"},
        "tracker": tracker,
        "filesystem": filesystem,
        "summary": payload.get("detail"),
    }
    return json.dumps(evidence, ensure_ascii=False)


def _upsert_issue(
    session: Session,
    *,
    watch_id: str,
    kind: SeedingIssueKind,
    score: int,
    remaining: float | None,
    evidence: str,
    now: datetime,
) -> bool:
    active = session.scalars(
        select(SeedingHealthIssue).where(
            SeedingHealthIssue.watch_id == watch_id,
            SeedingHealthIssue.kind == kind.value,
            SeedingHealthIssue.status.in_([item.value for item in ACTIVE_ISSUE_STATUSES]),
        )
    ).first()
    if active is not None:
        active.detect_count += 1
        active.last_detected_at = now
        active.risk_score = score
        active.severity = severity_for(score).value
        active.hnr_remaining_hours = remaining
        active.evidence = evidence
        active.updated_at = now
        return False

    recent = session.scalars(
        select(SeedingHealthIssue).where(
            SeedingHealthIssue.watch_id == watch_id,
            SeedingHealthIssue.kind == kind.value,
            SeedingHealthIssue.status == SeedingIssueStatus.RESOLVED.value,
            SeedingHealthIssue.resolved_at.is_not(None),
            SeedingHealthIssue.resolved_at > now - timedelta(days=7),
        )
    ).first()
    if recent is not None:
        recent.status = status_on_redetect(SeedingIssueStatus.RESOLVED).value
        recent.detect_count += 1
        recent.last_detected_at = now
        recent.risk_score = score
        recent.severity = severity_for(score).value
        recent.hnr_remaining_hours = remaining
        recent.evidence = evidence
        recent.resolved_at = None
        recent.resolved_reason = None
        recent.updated_at = now
        return False

    session.add(
        SeedingHealthIssue(
            id=new_uuid(),
            watch_id=watch_id,
            kind=kind.value,
            severity=severity_for(score).value,
            risk_score=score,
            hnr_remaining_hours=remaining,
            status=SeedingIssueStatus.OPEN.value,
            evidence=evidence,
            suggestion=suggestion_for(kind).value,
            detect_count=1,
            first_detected_at=now,
            last_detected_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    return True


def _clear_issue(session: Session, watch_id: str, kind: SeedingIssueKind, now: datetime) -> bool:
    active = session.scalars(
        select(SeedingHealthIssue).where(
            SeedingHealthIssue.watch_id == watch_id,
            SeedingHealthIssue.kind == kind.value,
            SeedingHealthIssue.status.in_([item.value for item in ACTIVE_ISSUE_STATUSES]),
        )
    ).first()
    if active is None:
        return False
    active.status = status_on_clear(SeedingIssueStatus(active.status)).value
    active.resolved_at = now
    active.resolved_reason = SeedingIssueResolution.AUTO.value
    active.updated_at = now
    return True
