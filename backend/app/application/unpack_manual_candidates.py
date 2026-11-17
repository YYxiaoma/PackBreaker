from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter, SiteView
from backend.app.application.unpack_manual_torrents import (
    ManualTorrentStore,
    ManualTorrentStoreError,
)
from backend.app.application.unpack_matching import _task_unit_from_item
from backend.app.domain.media_matching import MediaDescriptor, MediaFileSummary, parse_media_name
from backend.app.domain.site_search import CandidateMeta
from backend.app.domain.torrent import TorrentMeta
from backend.app.domain.unpack import UnpackItemStatus
from backend.app.domain.unpack_matching import assess_unpack_candidate
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


class UnpackManualSiteProvider(Protocol):
    def get(self, site_id: str) -> SiteView: ...

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...


@dataclass(frozen=True, slots=True)
class ManualCandidatePreview:
    site_id: str
    site_name: str
    source_kind: str
    source_title: str
    source_size_bytes: int
    candidate_key: str
    candidate_title: str
    torrent_name: str
    torrent_kind: str
    v1_info_hash: str | None
    v2_info_hash: str | None
    metainfo_digest: str
    payload_sha256: str
    total_size_bytes: int
    file_count: int
    files: tuple[dict[str, Any], ...]
    score_bps: int
    evidence: dict[str, Any]
    hard_conflicts: tuple[str, ...]
    seeders: int | None
    imdb_id: str | None
    douban_id: str | None


@dataclass(frozen=True, slots=True)
class ManualCandidateAddResult:
    candidate_id: str
    candidate_key: str
    generation: int
    item_version: int


@dataclass(frozen=True, slots=True)
class _ReviewContext:
    item_version: int
    generation: int
    source_title: str
    source_size_bytes: int
    source_descriptor: MediaDescriptor
    frozen_site_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedCandidate:
    context: _ReviewContext
    site: SiteView
    binding: EnabledSiteAdapter
    source_kind: str
    candidate_key: str
    candidate_title: str
    candidate_descriptor: MediaDescriptor
    torrent_meta: TorrentMeta
    torrent_content: bytes
    seeders: int | None
    imdb_id: str | None
    douban_id: str | None
    assessment: Any


class UnpackManualCandidateService:
    """Preview and explicitly add a manually selected torrent to the current review generation."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackManualSiteProvider,
        torrent_store: ManualTorrentStore,
        *,
        fetch_timeout_seconds: float = 180.0,
    ) -> None:
        if fetch_timeout_seconds <= 0:
            raise ValueError("manual candidate fetch timeout must be positive")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._torrent_store = torrent_store
        self._fetch_timeout_seconds = fetch_timeout_seconds

    async def preview(
        self,
        item_id: str,
        *,
        site_id: str,
        expected_generation: int,
        expected_item_version: int,
        source_url: str | None,
        torrent_content: bytes | None,
    ) -> ManualCandidatePreview:
        resolved = await self._resolve(
            item_id,
            site_id=site_id,
            expected_generation=expected_generation,
            expected_item_version=expected_item_version,
            source_url=source_url,
            torrent_content=torrent_content,
        )
        return self._preview_view(resolved)

    async def add(
        self,
        item_id: str,
        *,
        site_id: str,
        expected_generation: int,
        expected_item_version: int,
        expected_metainfo_digest: str,
        expected_payload_sha256: str,
        source_url: str | None,
        torrent_content: bytes | None,
    ) -> ManualCandidateAddResult:
        resolved = await self._resolve(
            item_id,
            site_id=site_id,
            expected_generation=expected_generation,
            expected_item_version=expected_item_version,
            source_url=source_url,
            torrent_content=torrent_content,
        )
        if resolved.torrent_meta.metainfo_digest != expected_metainfo_digest:
            raise self._conflict("种子元信息在预览后发生变化，请重新预览并确认")
        payload_sha256 = ManualTorrentStore.key_for(resolved.torrent_content)
        if payload_sha256 != expected_payload_sha256:
            raise self._conflict("种子文件内容在预览后发生变化，请重新预览并确认")

        try:
            storage_key = self._torrent_store.store(resolved.torrent_content)
        except ManualTorrentStoreError as exc:
            raise self._invalid("无法安全保存已确认的种子文件，请重新预览并确认") from exc

        try:
            with self._session_factory() as session:
                begin_immediate_write(session)
                item = session.get(UnpackExecutionItem, item_id)
                if item is None:
                    raise self._not_found()
                execution = session.get(UnpackExecution, item.execution_id)
                if execution is None:
                    raise self._not_found()
                self._assert_reviewable(
                    session,
                    item,
                    execution,
                    site_id=site_id,
                    expected_generation=expected_generation,
                    expected_item_version=expected_item_version,
                )
                existing = session.scalar(
                    select(UnpackMatchCandidate)
                    .where(UnpackMatchCandidate.item_id == item.id)
                    .where(UnpackMatchCandidate.generation == item.candidate_generation)
                    .where(UnpackMatchCandidate.site_id == site_id)
                    .where(UnpackMatchCandidate.candidate_key == resolved.candidate_key)
                    .limit(1)
                )
                now = utc_now()
                raw_ref: dict[str, Any] = {
                    "torrent_id": resolved.candidate_key,
                    "adapter_site_id": resolved.binding.site_id,
                    "site_config_id": site_id,
                    "site_config_version": resolved.binding.config_version,
                    "manual_candidate": True,
                    "manual_source_kind": resolved.source_kind,
                }
                raw_ref["manual_torrent_storage_key"] = storage_key
                evidence = {
                    **resolved.assessment.evidence,
                    "manual_candidate": True,
                    "manual_source_kind": resolved.source_kind,
                    "metainfo_digest": resolved.torrent_meta.metainfo_digest,
                }
                if existing is None:
                    existing = UnpackMatchCandidate(
                        id=new_uuid(),
                        item_id=item.id,
                        generation=item.candidate_generation,
                        site_id=site_id,
                        candidate_key=resolved.candidate_key,
                        title=resolved.candidate_title,
                        size_bytes=sum(
                            entry.length
                            for entry in resolved.torrent_meta.files
                            if not entry.padding
                        ),
                        imdb_id=resolved.imdb_id,
                        douban_id=resolved.douban_id,
                        seeders=resolved.seeders,
                        score_bps=resolved.assessment.score_bps,
                        is_exact_match=resolved.assessment.is_exact_match,
                        evidence=evidence,
                        verification_status="NOT_CHECKED",
                        metainfo_digest=resolved.torrent_meta.metainfo_digest,
                        raw_ref=raw_ref,
                        created_at=now,
                    )
                    session.add(existing)
                else:
                    existing.title = resolved.candidate_title
                    existing.size_bytes = sum(
                        entry.length for entry in resolved.torrent_meta.files if not entry.padding
                    )
                    existing.imdb_id = resolved.imdb_id
                    existing.douban_id = resolved.douban_id
                    existing.seeders = resolved.seeders
                    existing.score_bps = resolved.assessment.score_bps
                    existing.is_exact_match = resolved.assessment.is_exact_match
                    existing.evidence = evidence
                    existing.metainfo_digest = resolved.torrent_meta.metainfo_digest
                    existing.raw_ref = raw_ref
                    existing.verification_status = "NOT_CHECKED"
                    existing.verification_level = None
                    existing.verification_error_code = None
                item.version += 1
                item.updated_at = now
                session.commit()
                return ManualCandidateAddResult(
                    candidate_id=existing.id,
                    candidate_key=existing.candidate_key,
                    generation=item.candidate_generation,
                    item_version=item.version,
                )
        except Exception:
            if storage_key is not None:
                self._delete_if_unreferenced(storage_key)
            raise

    async def _resolve(
        self,
        item_id: str,
        *,
        site_id: str,
        expected_generation: int,
        expected_item_version: int,
        source_url: str | None,
        torrent_content: bytes | None,
    ) -> _ResolvedCandidate:
        if not site_id.strip():
            raise self._invalid("请选择扫描范围内的站点")
        has_url = bool(source_url and source_url.strip())
        has_file = torrent_content is not None
        if has_url == has_file:
            raise self._invalid("请填写一个种子详情页 URL，或选择一个 .torrent 文件")
        if torrent_content is not None and (
            not torrent_content or len(torrent_content) > self._torrent_store.max_bytes
        ):
            raise self._invalid("种子文件为空或超过 32 MiB 限制")

        context = self._review_context(
            item_id,
            site_id=site_id,
            expected_generation=expected_generation,
            expected_item_version=expected_item_version,
        )
        try:
            site = self._site_provider.get(site_id)
        except ApplicationError:
            raise
        if not site.enabled:
            raise self._conflict("所选站点未启用，请先启用该站点或选择其他站点")
        binding = next(
            (
                entry
                for entry in self._site_provider.enabled_adapters()
                if entry.config_id == site_id
            ),
            None,
        )
        if binding is None:
            raise self._conflict("所选站点当前没有可用的适配器")
        if site_id not in context.frozen_site_ids:
            raise self._conflict("手动添加候选的站点必须属于当前拆包任务的扫描范围")

        candidate_meta: CandidateMeta | None = None
        if has_url:
            torrent_id = self._torrent_id_from_details_url(source_url or "", site.base_url)
            try:
                async with asyncio.timeout(self._fetch_timeout_seconds):
                    details = await binding.adapter.fetch_details(torrent_id)
                    payload = await binding.adapter.fetch_torrent(torrent_id)
            except TimeoutError as exc:
                raise self._conflict("读取站点种子信息超时，请稍后重试") from exc
            except SiteAdapterError as exc:
                raise self._conflict(
                    "站点未能返回种子详情或种子文件，请检查链接与站点状态"
                ) from exc
            candidate_meta = details.candidate
            if (
                candidate_meta.site_id != binding.site_id
                or candidate_meta.torrent_id != torrent_id
                or payload.site_id != binding.site_id
                or payload.torrent_id != torrent_id
            ):
                raise self._conflict("站点返回的种子身份与输入链接不一致")
            content = payload.content
            source_kind = "URL"
            candidate_key = torrent_id
        else:
            content = torrent_content or b""
            source_kind = "FILE"
            candidate_key = "manual-" + hashlib.sha256(content).hexdigest()

        try:
            meta = parse_torrent(content)
        except Exception as exc:
            raise self._invalid("无法解析种子文件；请确认文件是有效的 .torrent 元信息") from exc

        file_summary = tuple(
            MediaFileSummary(entry.path, entry.length) for entry in meta.files if not entry.padding
        )
        total_size = sum(entry.length for entry in meta.files if not entry.padding)
        if candidate_meta is not None:
            descriptor = replace(
                candidate_meta.descriptor,
                total_size=total_size,
                files=file_summary,
            )
            title = candidate_meta.display_name
            seeders = candidate_meta.seeders
            imdb_id = self._external_id(descriptor, "imdb")
            douban_id = self._external_id(descriptor, "douban")
        else:
            descriptor = parse_media_name(
                meta.display_name,
                total_size=total_size,
                files=file_summary,
            )
            title = meta.display_name
            seeders = None
            imdb_id = self._external_id(descriptor, "imdb")
            douban_id = self._external_id(descriptor, "douban")

        assessment = assess_unpack_candidate(context.source_descriptor, descriptor)
        return _ResolvedCandidate(
            context=context,
            site=site,
            binding=binding,
            source_kind=source_kind,
            candidate_key=candidate_key,
            candidate_title=title,
            candidate_descriptor=descriptor,
            torrent_meta=meta,
            torrent_content=content,
            seeders=seeders,
            imdb_id=imdb_id,
            douban_id=douban_id,
            assessment=assessment,
        )

    def _review_context(
        self,
        item_id: str,
        *,
        site_id: str,
        expected_generation: int,
        expected_item_version: int,
    ) -> _ReviewContext:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._not_found()
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._not_found()
            self._assert_reviewable(
                session,
                item,
                execution,
                site_id=site_id,
                expected_generation=expected_generation,
                expected_item_version=expected_item_version,
            )
            unit = _task_unit_from_item(item)
            return _ReviewContext(
                item_version=item.version,
                generation=item.candidate_generation,
                source_title=PurePosixPath(unit.source_relative_path).name,
                source_size_bytes=unit.length,
                source_descriptor=unit.descriptor,
                frozen_site_ids=tuple(
                    value
                    for value in execution.config_snapshot.get("site_ids", [])
                    if isinstance(value, str)
                ),
            )

    @staticmethod
    def _assert_reviewable(
        session: Session,
        item: UnpackExecutionItem,
        execution: UnpackExecution,
        *,
        site_id: str,
        expected_generation: int,
        expected_item_version: int,
    ) -> None:
        if item.version != expected_item_version:
            raise ApplicationError(
                code="UNPACK_MANUAL_CANDIDATE_VERSION_CONFLICT",
                status=409,
                title="审核内容已更新",
                detail="影片项已变化，请刷新审核弹窗后重新预览",
            )
        if item.candidate_generation != expected_generation:
            raise ApplicationError(
                code="UNPACK_MANUAL_CANDIDATE_GENERATION_CONFLICT",
                status=409,
                title="候选列表已更新",
                detail="候选代次已变化，请刷新审核弹窗后重新预览",
            )
        if item.status != UnpackItemStatus.REVIEW_REQUIRED.value:
            raise ApplicationError(
                code="UNPACK_MANUAL_CANDIDATE_NOT_REVIEWABLE",
                status=409,
                title="影片当前不需要人工审核",
                detail="仅待人工审核的影片可以手动添加候选",
            )
        if session.scalar(
            select(UnpackExternalOperationJournal.id)
            .where(UnpackExternalOperationJournal.item_id == item.id)
            .limit(1)
        ):
            raise ApplicationError(
                code="UNPACK_MANUAL_CANDIDATE_EXTERNAL_EFFECT",
                status=409,
                title="影片已进入外部操作阶段",
                detail="存在下载器或文件系统操作记录，不能更换匹配候选",
            )
        frozen_site_ids = execution.config_snapshot.get("site_ids")
        if (
            not isinstance(frozen_site_ids, list)
            or any(not isinstance(value, str) for value in frozen_site_ids)
            or site_id not in frozen_site_ids
        ):
            raise ApplicationError(
                code="UNPACK_MANUAL_CANDIDATE_SITE_OUT_OF_SCOPE",
                status=409,
                title="站点不在任务扫描范围内",
                detail="手动添加候选只能使用当前拆包任务已配置的扫描站点",
            )

    @staticmethod
    def _torrent_id_from_details_url(value: str, base_url: str) -> str:
        try:
            parsed = urlsplit(value.strip())
            base = urlsplit(base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or parsed.scheme.casefold() != base.scheme.casefold()
                or (parsed.hostname or "").casefold() != (base.hostname or "").casefold()
                or parsed.port != base.port
                or PurePosixPath(parsed.path).name.casefold() != "details.php"
            ):
                raise ValueError
            values = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
            if len(values) != 1 or re.fullmatch(r"[0-9]{1,20}", values[0]) is None:
                raise ValueError
            return values[0]
        except (TypeError, ValueError) as exc:
            raise ApplicationError(
                code="UNPACK_MANUAL_CANDIDATE_URL_INVALID",
                status=422,
                title="种子详情链接无效",
                detail="请输入所选站点自身的 details.php?id=数字 链接，不支持任意外部 URL",
            ) from exc

    def _preview_view(self, resolved: _ResolvedCandidate) -> ManualCandidatePreview:
        meta = resolved.torrent_meta
        files = tuple(
            {"path": entry.path, "size_bytes": entry.length}
            for entry in meta.files
            if not entry.padding
        )
        hard_conflicts = resolved.assessment.evidence.get("hard_conflicts", [])
        return ManualCandidatePreview(
            site_id=resolved.site.id,
            site_name=resolved.site.name,
            source_kind=resolved.source_kind,
            source_title=resolved.context.source_title,
            source_size_bytes=resolved.context.source_size_bytes,
            candidate_key=resolved.candidate_key,
            candidate_title=resolved.candidate_title,
            torrent_name=meta.display_name,
            torrent_kind=meta.torrent_kind.value,
            v1_info_hash=meta.v1_info_hash,
            v2_info_hash=meta.v2_info_hash,
            metainfo_digest=meta.metainfo_digest,
            payload_sha256=ManualTorrentStore.key_for(resolved.torrent_content),
            total_size_bytes=sum(entry.length for entry in meta.files if not entry.padding),
            file_count=len(files),
            files=files[:100],
            score_bps=resolved.assessment.score_bps,
            evidence=dict(resolved.assessment.evidence),
            hard_conflicts=tuple(value for value in hard_conflicts if isinstance(value, str))
            if isinstance(hard_conflicts, list)
            else (),
            seeders=resolved.seeders,
            imdb_id=resolved.imdb_id,
            douban_id=resolved.douban_id,
        )

    @staticmethod
    def _external_id(descriptor: MediaDescriptor, namespace: str) -> str | None:
        return next(
            (value.value for value in descriptor.external_ids if value.namespace == namespace),
            None,
        )

    def _delete_if_unreferenced(self, storage_key: str) -> None:
        try:
            with self._session_factory() as session:
                candidates = session.scalars(select(UnpackMatchCandidate)).all()
                referenced = any(
                    isinstance(candidate.raw_ref, dict)
                    and any(
                        candidate.raw_ref.get(field) == storage_key
                        for field in ("manual_torrent_storage_key", "verified_torrent_storage_key")
                    )
                    for candidate in candidates
                )
            if not referenced:
                self._torrent_store.delete(storage_key)
        except Exception:
            # The stored object is content-addressed and private; a later cleanup can
            # safely retry without affecting any referenced torrent.
            return

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MANUAL_CANDIDATE_INVALID",
            status=422,
            title="手动添加的种子无效",
            detail=detail,
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MANUAL_CANDIDATE_CONFLICT",
            status=409,
            title="无法添加手动候选",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_NOT_FOUND",
            status=404,
            title="数据拆包影片项不存在",
            detail="未找到指定影片项",
        )
