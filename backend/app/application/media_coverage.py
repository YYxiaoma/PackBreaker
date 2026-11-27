"""监控中心 · 辅种覆盖率报告的只读扫描与查询用例。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.media_coverage import (
    coverage_bucket,
    coverage_suggestion,
    descriptor_movie_key,
    external_ids_movie_key,
)
from backend.app.domain.media_matching import (
    MediaDescriptor,
    media_file_token_signature,
    parse_media_name,
)
from backend.app.domain.task_definition import DEFAULT_VIDEO_EXTENSIONS
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.models import (
    MediaCoverageMovie,
    MediaCoverageRun,
    MediaCoverageScope,
    MediaCoverageTorrent,
    SeedingWatch,
    UnpackTemplateMatchAcceptance,
    UnpackTorrentMetadata,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.source_inventory import scan_source_inventory

_video_extensions = {item.casefold() for item in DEFAULT_VIDEO_EXTENSIONS}

# 合集特征：一条种子包含多部影片时，单条种子必须能记到多部影片，否则合集内
# 除评分最高的一部外都会被误判成「完全没做种」。
_PACK_MARKERS = (
    "合集",
    "套装",
    "系列",
    "collection",
    "trilogy",
    "anthology",
    "boxset",
    "box set",
    "complete",
)
_PACK_YEAR_RANGE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-~—]\s*((?:19|20)\d{2})(?!\d)")
_YEAR_TOKEN_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_PACK_MATCH_LEVELS = frozenset({"CONTENT_PATH", "COLLECTION"})
# 目录内容与种子大小应基本一致；超出容差说明目录混入了别的种子内容，
# 或用户只勾选了部分文件，此时宁可回退到名称匹配也不批量记账。
_DIRECTORY_SIZE_RATIO = (0.95, 1.15)
# 单影片目录带字幕、样片、nfo 等非视频文件时，视频总字节会明显小于种子大小。
_SINGLE_DIRECTORY_SIZE_RATIO = (0.9, 1.25)
# 站点列表只有标题和大小，没有本地路径，容差放宽以免漏掉只收藏了部分影片的合集。
_PACK_NAME_SIZE_RATIO = (0.9, 1.6)
_TECH_TOKENS = frozenset(
    {
        "bluray",
        "blu",
        "ray",
        "remux",
        "webdl",
        "webrip",
        "hdtv",
        "hdctv",
        "hevc",
        "avc",
        "av1",
        "1080p",
        "720p",
        "2160p",
        "4k",
        "hdr",
        "hdr10",
        "hdr10plus",
        "dolbyvision",
        "dovi",
        "ac3",
        "aac",
        "flac",
        "lpcm",
        "atmos",
        "truehd",
        "dtshd",
        "eac3",
        "ddp",
        "mnhd",
        "muhd",
        "uhd",
        "frds",
        "greenotea",
        "10bit",
        "8bit",
        "12bit",
        "2audio",
        "3audio",
        "4audio",
        "chs",
        "cht",
        "zhcn",
        "zhtw",
        "eng",
        "english",
        "jpn",
        "japanese",
        "kor",
        "korean",
        "repack",
        "proper",
        "mkv",
        "mp4",
        "avi",
        "mov",
        "webm",
        "ts",
    }
)


def _has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _distinctive_tokens(tokens: set[str]) -> set[str]:
    """剔除技术标记与纯数字，只留下能证明「同一部影片」的标题词。"""

    return {
        token
        for token in tokens
        if token not in _TECH_TOKENS
        and not token.isdigit()
        and (len(token) >= 2 or _has_cjk(token))
    }


@dataclass(frozen=True, slots=True)
class CoverageRunView:
    id: str
    scope_label: str
    status: str
    phase: str
    started_at: datetime
    finished_at: datetime | None
    movie_count: int
    site_count: int
    watched_count: int
    identity_resolved_count: int
    degraded_sites: list[str]
    error_code: str | None


@dataclass(frozen=True, slots=True)
class CoverageScopeView:
    selection_token: str
    scope_label: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CoverageSummaryView:
    generated_at: datetime
    last_run: CoverageRunView | None
    total_movies: int
    buckets: dict[str, int]
    known_site_count: int
    watched_torrent_count: int
    low_confidence_count: int


@dataclass(frozen=True, slots=True)
class CoverageMovieView:
    movie_key: str
    display_title: str
    year: int | None
    resolution: str | None
    site_keys: list[str]
    coverage_count: int
    bucket: str
    match_level: str
    confidence: str
    suggestion: str
    relative_path_hint: str | None


@dataclass(frozen=True, slots=True)
class CoverageMovieListView:
    items: list[CoverageMovieView]
    total: int
    page: int
    page_size: int


class MediaCoverageService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        path_scope: AuthorizedPathScope,
        site_service: Any,
        site_probe_enabled: bool,
        site_probe_budget: int,
        max_site_items: int,
        max_movies: int,
    ) -> None:
        self._session_factory = session_factory
        self._path_scope = path_scope
        self._site_service = site_service
        self._site_probe_enabled = site_probe_enabled
        self._site_probe_budget = max(1, min(site_probe_budget, 200))
        self._max_site_items = max(100, min(max_site_items, 100000))
        self._max_movies = max(100, min(max_movies, 200000))
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def decode_selection_token(token: str) -> str:
        if not token.startswith("v1.") or len(token) > 8192:
            raise ApplicationError(
                code="MEDIA_COVERAGE_INVALID_SCOPE",
                status=422,
                title="媒体库范围无效",
                detail="目录选择令牌格式无效",
            )
        raw = token[3:]
        try:
            padding = "=" * (-len(raw) % 4)
            path = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise ApplicationError(
                code="MEDIA_COVERAGE_INVALID_SCOPE",
                status=422,
                title="媒体库范围无效",
                detail="目录选择令牌无法解码",
            ) from exc
        if not path:
            raise ApplicationError(
                code="MEDIA_COVERAGE_INVALID_SCOPE",
                status=422,
                title="媒体库范围无效",
                detail="目录选择令牌为空",
            )
        return path

    def get_scope(self) -> CoverageScopeView | None:
        with self._session_factory() as session:
            row = session.get(MediaCoverageScope, "default")
            if row is None:
                return None
            return CoverageScopeView(
                selection_token=row.selection_token,
                scope_label=row.scope_label,
                updated_at=row.updated_at,
            )

    def save_scope(self, selection_token: str, scope_label: str) -> CoverageScopeView:
        raw_path = self.decode_selection_token(selection_token)
        try:
            self._path_scope.resolve_existing_directory(raw_path)
        except DomainViolation as exc:
            raise ApplicationError(
                code="MEDIA_COVERAGE_INVALID_SCOPE",
                status=422,
                title="媒体库范围无效",
                detail=str(exc),
            ) from exc
        label = scope_label.strip()[:255] or "授权媒体库"
        now = utc_now()
        with self._session_factory() as session:
            row = session.get(MediaCoverageScope, "default")
            if row is None:
                row = MediaCoverageScope(
                    id="default",
                    selection_token=selection_token,
                    scope_label=label,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.selection_token = selection_token
                row.scope_label = label
                row.updated_at = now
            session.commit()
        return CoverageScopeView(selection_token=selection_token, scope_label=label, updated_at=now)

    async def create_run(self, selection_token: str) -> str:
        raw_path = self.decode_selection_token(selection_token)
        if self._lock.locked():
            raise ApplicationError(
                code="MEDIA_COVERAGE_SCAN_IN_PROGRESS",
                status=409,
                title="覆盖率扫描正在进行",
                detail="同一时间只允许运行一次覆盖率扫描",
            )
        try:
            reference, resolved = self._path_scope.resolve_existing_directory(raw_path)
        except DomainViolation as exc:
            raise ApplicationError(
                code="MEDIA_COVERAGE_INVALID_SCOPE",
                status=422,
                title="媒体库范围无效",
                detail=str(exc),
            ) from exc

        await self._lock.acquire()
        try:
            run_id = new_uuid()
            scope_fingerprint = sha256(reference.encode("utf-8")).hexdigest()
            scope_label = Path(reference).name or "授权媒体库"
            now = utc_now()
            with self._session_factory() as session:
                session.add(
                    MediaCoverageRun(
                        id=run_id,
                        scope_fingerprint=scope_fingerprint,
                        scope_label=scope_label,
                        status="RUNNING",
                        phase="PENDING",
                        started_at=now,
                    )
                )
                session.commit()
            task = asyncio.create_task(
                self._execute(run_id, resolved, scope_fingerprint, scope_label)
            )
        except Exception:
            self._lock.release()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    def reconcile_stale_runs(self) -> None:
        """Mark runs left RUNNING by a previous process as interrupted."""
        with self._session_factory() as session:
            stale = list(
                session.scalars(
                    select(MediaCoverageRun).where(MediaCoverageRun.status == "RUNNING")
                )
            )
            for run in stale:
                run.status = "FAILED"
                run.phase = "FAILED"
                run.error_code = "INTERRUPTED"
                run.finished_at = utc_now()
            if stale:
                session.commit()

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.reconcile_stale_runs()

    async def _execute(
        self, run_id: str, root: Path, scope_fingerprint: str, scope_label: str
    ) -> None:
        try:
            main_loop = asyncio.get_running_loop()
            await asyncio.to_thread(
                self._scan_sync, run_id, root, scope_fingerprint, scope_label, main_loop
            )
        except Exception as exc:  # noqa: BLE001 - keep the run record authoritative
            with self._session_factory() as session:
                run = session.get(MediaCoverageRun, run_id)
                if run is not None:
                    run.status = "FAILED"
                    run.phase = "FAILED"
                    run.error_code = type(exc).__name__[:64]
                    run.finished_at = utc_now()
                    session.commit()
        finally:
            self._lock.release()

    def _set_phase(self, run_id: str, phase: str) -> None:
        with self._session_factory() as session:
            run = session.get(MediaCoverageRun, run_id)
            if run is not None:
                run.phase = phase
                session.commit()

    def _scan_sync(
        self,
        run_id: str,
        root: Path,
        scope_fingerprint: str,
        scope_label: str,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._set_phase(run_id, "SCANNING_LIBRARY")
        inventory = scan_source_inventory(root, max_files=self._max_movies)
        movies: dict[str, dict[str, Any]] = {}
        by_basename: dict[str, str] = {}
        by_size: dict[int, str] = {}
        by_token: dict[str, set[str]] = {}
        # 目录 → 目录内影片集合 / 视频总字节，用于识别「一条合集种子包含多部影片」。
        dir_keys: dict[str, set[str]] = {}
        dir_bytes: dict[str, int] = {}
        # 同一 inode 可能被硬链接到多个影片目录；容量校验要与种子大小同口径，
        # 只累计一次物理内容，否则合集目录会被算大而拒绝归属。
        dir_inodes: dict[str, set[tuple[int, int]]] = {}
        # 标题 token 一次算好；每轮匹配要比较上千次，重复解析发布名会拖慢扫描。
        tokens_by_movie: dict[str, set[str]] = {}
        for item in inventory:
            suffix = PurePosixPath(item.relative_path).suffix.casefold()
            if suffix not in _video_extensions:
                continue
            basename = PurePosixPath(item.relative_path).name
            descriptor = parse_media_name(basename, total_size=item.length)
            key = descriptor_movie_key(descriptor)
            record = movies.get(key)
            if record is None:
                title = " ".join(descriptor.title_tokens) or descriptor.raw_name
                record = {
                    "movie_key": key,
                    "display_title": title[:512],
                    "year": descriptor.year,
                    "resolution": descriptor.resolution,
                    "external_ids": [
                        {"namespace": value.namespace, "value": value.value}
                        for value in descriptor.external_ids
                    ],
                    "relative_path_hint": item.relative_path,
                    "size_bytes": 0,
                    # 硬链接会把同一物理文件算两次；与目录容量同口径，这里也按 inode 去重。
                    "inode_bytes": 0,
                    "inodes": set(),
                    "sites": set(),
                    "unknown_observations": 0,
                    "confidence": "HIGH" if descriptor.external_ids else "MEDIUM",
                    "match_level": "EXTERNAL_ID" if descriptor.external_ids else "TITLE_YEAR",
                    "evidence": {"matched_name": basename},
                }
                movies[key] = record
            record["size_bytes"] += item.length
            inode = (item.snapshot.device, item.snapshot.inode)
            if inode not in record["inodes"]:
                record["inodes"].add(inode)
                record["inode_bytes"] += item.length
            relative = PurePosixPath(item.relative_path)
            for parent in relative.parents:
                directory = parent.as_posix()
                if directory in {"", "."}:
                    break
                dir_keys.setdefault(directory, set()).add(key)
                seen_inodes = dir_inodes.setdefault(directory, set())
                if inode not in seen_inodes:
                    seen_inodes.add(inode)
                    dir_bytes[directory] = dir_bytes.get(directory, 0) + item.length
            by_basename.setdefault(basename.casefold(), key)
            by_size.setdefault(item.length, key)
            tokens = set(media_file_token_signature(basename))
            tokens_by_movie.setdefault(key, set()).update(tokens)
            for token in tokens:
                by_token.setdefault(token, set()).add(key)

        self._set_phase(run_id, "LOADING_EVIDENCE")
        with self._session_factory() as session:
            accepted = {
                (row.site_key, row.candidate_remote_id): row.source_relative_path
                for row in session.scalars(select(UnpackTemplateMatchAcceptance))
            }
            metadata: dict[tuple[str, str], str | None] = {}
            for row in session.scalars(select(UnpackTorrentMetadata)):
                metadata_key = external_ids_movie_key(row.external_ids or [])
                if metadata_key is None:
                    descriptor_payload = row.descriptor or {}
                    try:
                        parsed = MediaDescriptor(**descriptor_payload)
                        metadata_key = descriptor_movie_key(parsed)
                    except (TypeError, ValueError):
                        metadata_key = None
                metadata[(row.site_key, row.remote_torrent_id)] = metadata_key
            watches = list(
                session.scalars(select(SeedingWatch).where(SeedingWatch.presence == "ACTIVE"))
            )

        observations: list[dict[str, Any]] = []
        seen_observations: set[tuple[str, str, str]] = set()
        for watch in watches:
            matches = self._match_watch_movies(
                watch,
                movies,
                by_basename,
                by_size,
                by_token,
                tokens_by_movie,
                accepted,
                metadata,
                dir_keys,
                dir_bytes,
            )
            for movie_key, level, confidence in matches:
                appended = self._append_observation(
                    observations,
                    seen_observations,
                    movie_key=movie_key,
                    downloader_id=watch.downloader_id,
                    torrent_hash=watch.torrent_hash,
                    site_key=watch.site_key,
                    remote_torrent_id=watch.remote_torrent_id,
                    level=level,
                    confidence=confidence,
                    evidence={
                        "source": "SEEDING_WATCH",
                        "name": watch.torrent_name[:200],
                        "pack": level in _PACK_MATCH_LEVELS,
                    },
                )
                if appended:
                    self._attach_observation(movies[movie_key], watch.site_key, level, confidence)

        degraded: list[str] = []
        if self._site_probe_enabled:
            self._set_phase(run_id, "PROBING_SITES")
            degraded = self._probe_sites(
                movies,
                by_basename,
                by_size,
                by_token,
                tokens_by_movie,
                accepted,
                metadata,
                observations,
                seen_observations,
                loop,
            )
        else:
            degraded.append("站点列表探测未启用")

        self._set_phase(run_id, "PERSISTING")
        # watched_count 统计种子条数而不是证据行数，合集种子会生成多行证据。
        seed_keys = {(item["downloader_id"], item["torrent_hash"]) for item in observations}
        watched_count = len(seed_keys)
        # 身份解析数只算真正定到站点的种子；站点未识别时这两项必然不同，
        # 恒等会让「身份解析率」这个指标失去意义。
        identity_resolved = len(
            {
                (item["downloader_id"], item["torrent_hash"])
                for item in observations
                if item["site_key"]
            }
        )
        site_count = len({item["site_key"] for item in observations if item["site_key"]})
        with self._session_factory() as session:
            for record in movies.values():
                site_keys = sorted(record["sites"])
                unknown_only = not site_keys and record["unknown_observations"] > 0
                bucket = coverage_bucket(len(site_keys), unknown_only=unknown_only)
                record["bucket"] = bucket
                record["sites"] = site_keys
                record["confidence"] = "LOW" if unknown_only else self._weaker(record["confidence"])
                record["suggestion"] = coverage_suggestion(bucket, confidence=record["confidence"])
                session.add(
                    MediaCoverageMovie(
                        run_id=run_id,
                        movie_key=record["movie_key"],
                        display_title=record["display_title"],
                        year=record["year"],
                        resolution=record["resolution"],
                        external_ids=record["external_ids"],
                        relative_path_hint=record["relative_path_hint"],
                        coverage_count=len(site_keys),
                        site_keys=site_keys,
                        bucket=bucket,
                        match_level=record["match_level"],
                        confidence=record["confidence"],
                        suggestion=record["suggestion"],
                        evidence=record["evidence"],
                    )
                )
            for observation in observations:
                session.add(
                    MediaCoverageTorrent(
                        run_id=run_id,
                        movie_key=observation["movie_key"],
                        downloader_id=observation["downloader_id"],
                        torrent_hash=observation["torrent_hash"],
                        site_key=observation["site_key"],
                        remote_torrent_id=observation["remote_torrent_id"],
                        match_level=observation["match_level"],
                        confidence=observation["confidence"],
                        evidence=observation["evidence"],
                    )
                )
            run = session.get(MediaCoverageRun, run_id)
            if run is not None:
                run.status = "COMPLETED"
                run.phase = "COMPLETED"
                run.finished_at = utc_now()
                run.movie_count = len(movies)
                run.site_count = site_count
                run.watched_count = watched_count
                run.identity_resolved_count = identity_resolved
                run.degraded_sites = degraded
            older = list(
                session.scalars(
                    select(MediaCoverageRun).order_by(MediaCoverageRun.started_at.desc()).offset(5)
                )
            )
            for stale in older:
                session.delete(stale)
            session.commit()

    @staticmethod
    def _weaker(current: str) -> str:
        return "LOW" if current == "LOW" else current

    @staticmethod
    def _attach_observation(
        movie: dict[str, Any], site_key: str | None, level: str, confidence: str
    ) -> None:
        if site_key:
            movie["sites"].add(site_key)
        else:
            movie["unknown_observations"] += 1
        if level == "EXTERNAL_ID" or level == "ACCEPTED_PATH" or level == "CONTENT_PATH":
            movie["match_level"] = level
            movie["confidence"] = "HIGH"
        elif (
            level in {"TITLE_YEAR", "TOKEN_SIGNATURE", "COLLECTION"}
            and movie["confidence"] != "HIGH"
        ):
            movie["match_level"] = level
            movie["confidence"] = "MEDIUM"

    @staticmethod
    def _append_observation(
        observations: list[dict[str, Any]],
        seen: set[tuple[str, str, str]],
        *,
        movie_key: str,
        downloader_id: str | None,
        torrent_hash: str,
        site_key: str | None,
        remote_torrent_id: str | None,
        level: str,
        confidence: str,
        evidence: dict[str, Any],
    ) -> bool:
        marker = (movie_key, downloader_id or "", torrent_hash)
        if marker in seen:
            return False
        seen.add(marker)
        observations.append(
            {
                "movie_key": movie_key,
                "downloader_id": downloader_id,
                "torrent_hash": torrent_hash,
                "site_key": site_key,
                "remote_torrent_id": remote_torrent_id,
                "match_level": level,
                "confidence": confidence,
                "evidence": evidence,
            }
        )
        return True

    def _match_watch_movies(
        self,
        watch: SeedingWatch,
        movies: dict[str, dict[str, Any]],
        by_basename: dict[str, str],
        by_size: dict[int, str],
        by_token: dict[str, set[str]],
        tokens_by_movie: dict[str, set[str]],
        accepted: dict[tuple[str, str], str],
        metadata: dict[tuple[str, str], str | None],
        dir_keys: dict[str, set[str]],
        dir_bytes: dict[str, int],
    ) -> list[tuple[str, str, str]]:
        """返回该种子覆盖的影片列表；合集种子会返回多部影片。"""

        key, level, confidence = self._resolve_movie_key(
            watch.torrent_name,
            watch.size_bytes,
            watch.site_key,
            watch.remote_torrent_id,
            movies,
            by_basename,
            by_size,
            accepted,
            metadata,
            by_token,
            tokens_by_movie,
        )
        if key and level in {"EXTERNAL_ID", "ACCEPTED_PATH"}:
            return [(key, level, confidence)]
        # 目录归属是最强的本地证据：content_path 指向的目录里实际放着这些影片文件。
        directory_keys = self._match_content_directory(watch, dir_keys, dir_bytes)
        if directory_keys:
            return [(item, "CONTENT_PATH", "HIGH") for item in directory_keys]
        pack_keys = self._match_pack_name(
            watch.torrent_name, watch.size_bytes, movies, by_token, tokens_by_movie
        )
        if pack_keys:
            return pack_keys
        if key:
            return [(key, level, confidence)]
        return []

    def _match_site_item_movies(
        self,
        title: str,
        size: int | None,
        site_key: str,
        remote_id: str,
        movies: dict[str, dict[str, Any]],
        by_basename: dict[str, str],
        by_size: dict[int, str],
        by_token: dict[str, set[str]],
        tokens_by_movie: dict[str, set[str]],
        accepted: dict[tuple[str, str], str],
        metadata: dict[tuple[str, str], str | None],
    ) -> list[tuple[str, str, str]]:
        key, level, confidence = self._resolve_movie_key(
            title,
            size,
            site_key,
            remote_id,
            movies,
            by_basename,
            by_size,
            accepted,
            metadata,
            by_token,
            tokens_by_movie,
        )
        if key and level in {"EXTERNAL_ID", "ACCEPTED_PATH"}:
            return [(key, level, confidence)]
        pack_keys = self._match_pack_name(title, size, movies, by_token, tokens_by_movie)
        if pack_keys:
            return pack_keys
        if key:
            return [(key, level, confidence)]
        return []

    @staticmethod
    def _suffix_directory(raw_path: str, dir_keys: dict[str, set[str]]) -> str | None:
        """把下载器侧的绝对路径映射到扫描根下的相对目录。

        下载器路径（/downloads2/...）与本机挂载点不一致，无法直接相减，
        因此从完整路径开始逐段去掉前缀，取第一个能在索引里命中的后缀。
        """

        normalized = raw_path.replace("\\", "/").strip()
        if not normalized:
            return None
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        for start in range(len(parts)):
            candidate = "/".join(parts[start:])
            if candidate in dir_keys:
                return candidate
        return None

    def _match_content_directory(
        self,
        watch: SeedingWatch,
        dir_keys: dict[str, set[str]],
        dir_bytes: dict[str, int],
    ) -> list[str]:
        if not watch.content_path:
            return []
        directory = self._suffix_directory(watch.content_path, dir_keys)
        if directory is None:
            return []
        keys = dir_keys.get(directory)
        if not keys:
            return []
        if len(keys) == 1:
            # 单影片目录也要校验：合集种子只把其中一部放进扫描根时，
            # 目录里只有那一部，按名字也能命中，但容量会明显小于种子。
            # 这里用更宽的容差，因为单影片目录常带字幕、样片等非视频文件。
            if self._size_within(
                watch.size_bytes, dir_bytes.get(directory), *_SINGLE_DIRECTORY_SIZE_RATIO
            ):
                return sorted(keys)
            return []
        if not self._size_within(
            watch.size_bytes, dir_bytes.get(directory), *_DIRECTORY_SIZE_RATIO
        ):
            return []
        return sorted(keys)

    def _match_pack_name(
        self,
        title: str,
        size: int | None,
        movies: dict[str, dict[str, Any]],
        by_token: dict[str, set[str]],
        tokens_by_movie: dict[str, set[str]],
    ) -> list[tuple[str, str, str]]:
        """站点列表拿不到本地路径，用合集特征 + 标题词 + 年份 + 容量验证多条命中。"""

        lowered = title.casefold()
        has_range = _PACK_YEAR_RANGE_RE.search(lowered) is not None
        if not has_range and not any(marker in lowered for marker in _PACK_MARKERS):
            return []
        torrent_tokens = set(media_file_token_signature(title))
        if not torrent_tokens:
            return []
        years = self._pack_years(title)
        candidates: set[str] = set()
        for token in torrent_tokens:
            candidates.update(by_token.get(token, set()))
        selected: list[str] = []
        for candidate in candidates:
            movie = movies.get(candidate)
            if movie is None:
                continue
            candidate_tokens = tokens_by_movie.get(candidate, set())
            if not candidate_tokens:
                continue
            shared = torrent_tokens & candidate_tokens
            union = torrent_tokens | candidate_tokens
            if len(shared) < 4 or not union:
                continue
            if len(shared) / len(union) < 0.45:
                continue
            distinctive = _distinctive_tokens(shared)
            if len(distinctive) < 3 and not any(_has_cjk(token) for token in distinctive):
                continue
            year = movie["year"]
            if years and year is not None and not (min(years) <= year <= max(years)):
                continue
            selected.append(candidate)
        if not selected:
            return []
        if size and size > 0:
            # 与目录容量同口径：硬链接只算一次，否则多目录共享内容会把候选总量算大。
            total = sum(
                movies[item].get("inode_bytes") or movies[item]["size_bytes"] for item in selected
            )
            if total > 0 and not self._size_within(size, total, *_PACK_NAME_SIZE_RATIO):
                return []
        return [(item, "COLLECTION", "MEDIUM") for item in sorted(selected)]

    @staticmethod
    def _pack_years(title: str) -> tuple[int, ...]:
        return tuple(sorted({int(match.group(1)) for match in _YEAR_TOKEN_RE.finditer(title)}))

    @staticmethod
    def _size_within(size: int | None, total: int | None, low: float, high: float) -> bool:
        if not size or not total or size <= 0 or total <= 0:
            return False
        return low <= size / total <= high

    def _resolve_movie_key(
        self,
        title: str,
        size: int | None,
        site_key: str | None,
        remote_id: str | None,
        movies: dict[str, dict[str, Any]],
        by_basename: dict[str, str],
        by_size: dict[int, str],
        accepted: dict[tuple[str, str], str],
        metadata: dict[tuple[str, str], str | None],
        by_token: dict[str, set[str]],
        tokens_by_movie: dict[str, set[str]],
    ) -> tuple[str | None, str, str]:
        if site_key and remote_id:
            key = metadata.get((site_key, remote_id))
            if key and key in movies:
                return key, "EXTERNAL_ID", "HIGH"
            relative = accepted.get((site_key, remote_id))
            if relative:
                if relative in movies:
                    return relative, "ACCEPTED_PATH", "HIGH"
                basename_key = by_basename.get(PurePosixPath(relative).name.casefold())
                if basename_key:
                    return basename_key, "ACCEPTED_PATH", "HIGH"
        descriptor = parse_media_name(title, total_size=size)
        key = descriptor_movie_key(descriptor)
        if key in movies:
            return key, "TITLE_YEAR", "MEDIUM"
        basename_key = by_basename.get(PurePosixPath(title).name.casefold())
        if basename_key:
            return basename_key, "NAME_SIZE", "LOW"
        if size is not None and size in by_size:
            return by_size[size], "NAME_SIZE", "LOW"

        # 中文前缀、不同压制组后缀会让标题 key 不同；用发布名 token 的重叠度兜底。
        torrent_tokens = set(media_file_token_signature(title))
        candidates: set[str] = set()
        for token in torrent_tokens:
            candidates.update(by_token.get(token, set()))
        best_key: str | None = None
        best_score = 0.0
        for candidate in candidates:
            candidate_tokens = tokens_by_movie.get(candidate, set())
            shared = len(torrent_tokens & candidate_tokens)
            union = len(torrent_tokens | candidate_tokens)
            score = shared / union if union else 0.0
            if shared >= 4 and score >= 0.45 and score > best_score:
                best_key = candidate
                best_score = score
        if best_key:
            return best_key, "TOKEN_SIGNATURE", "MEDIUM"
        return None, "UNKNOWN", "LOW"

    def _probe_sites(
        self,
        movies: dict[str, dict[str, Any]],
        by_basename: dict[str, str],
        by_size: dict[int, str],
        by_token: dict[str, set[str]],
        tokens_by_movie: dict[str, set[str]],
        accepted: dict[tuple[str, str], str],
        metadata: dict[tuple[str, str], str | None],
        observations: list[dict[str, Any]],
        seen_observations: set[tuple[str, str, str]],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> list[str]:
        degraded: list[str] = []
        try:
            bindings = list(self._site_service.enabled_adapters())[: self._site_probe_budget]
        except Exception:  # noqa: BLE001 - site availability must not break the report
            return ["站点适配器不可用"]
        for binding in bindings:
            site_key = binding.site_id
            list_seeding = getattr(binding.adapter, "list_seeding", None)
            if list_seeding is None:
                degraded.append(f"{site_key}:不支持站点列表")
                continue
            cursor: str | None = None
            collected = 0
            try:
                while collected < self._max_site_items:
                    page = list_seeding(cursor)
                    if asyncio.iscoroutine(page):
                        # 复用应用主循环，避免在线程里新建事件循环导致客户端跨循环失败。
                        page = (
                            asyncio.run_coroutine_threadsafe(page, loop).result()
                            if loop is not None
                            else asyncio.run(page)
                        )
                    if not getattr(page, "supported", False):
                        degraded.append(f"{site_key}:站点列表不可用")
                        break
                    for item in page.items:
                        matches = self._match_site_item_movies(
                            item.title,
                            item.size_bytes,
                            site_key,
                            str(item.torrent_id),
                            movies,
                            by_basename,
                            by_size,
                            by_token,
                            tokens_by_movie,
                            accepted,
                            metadata,
                        )
                        for movie_key, level, confidence in matches:
                            appended = self._append_observation(
                                observations,
                                seen_observations,
                                movie_key=movie_key,
                                downloader_id=None,
                                torrent_hash=f"site:{site_key}:{item.torrent_id}",
                                site_key=site_key,
                                remote_torrent_id=str(item.torrent_id),
                                level=level,
                                confidence=confidence,
                                evidence={
                                    "source": "SITE_SEEDING",
                                    "name": item.title[:200],
                                    "pack": level in _PACK_MATCH_LEVELS,
                                },
                            )
                            if appended:
                                self._attach_observation(
                                    movies[movie_key], site_key, level, confidence
                                )
                        collected += 1
                    cursor = page.next_cursor
                    if not cursor:
                        break
            except Exception:  # noqa: BLE001 - isolate site failures
                degraded.append(f"{site_key}:站点列表读取失败")
        return degraded

    @staticmethod
    def _run_view(row: MediaCoverageRun) -> CoverageRunView:
        return CoverageRunView(
            id=row.id,
            scope_label=row.scope_label,
            status=row.status,
            phase=row.phase,
            started_at=row.started_at,
            finished_at=row.finished_at,
            movie_count=row.movie_count,
            site_count=row.site_count,
            watched_count=row.watched_count,
            identity_resolved_count=row.identity_resolved_count,
            degraded_sites=list(row.degraded_sites or []),
            error_code=row.error_code,
        )

    def _latest_run(self, session: Session) -> MediaCoverageRun | None:
        return session.scalar(select(MediaCoverageRun).order_by(MediaCoverageRun.started_at.desc()))

    def runs(self, limit: int = 20) -> list[CoverageRunView]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(MediaCoverageRun)
                    .order_by(MediaCoverageRun.started_at.desc())
                    .limit(min(max(limit, 1), 100))
                )
            )
            return [self._run_view(row) for row in rows]

    def summary(self) -> CoverageSummaryView:
        with self._session_factory() as session:
            run = self._latest_run(session)
            if run is None:
                return CoverageSummaryView(
                    generated_at=utc_now(),
                    last_run=None,
                    total_movies=0,
                    buckets={key: 0 for key in ("ZERO", "ONE", "TWO", "THREE_PLUS", "UNKNOWN")},
                    known_site_count=0,
                    watched_torrent_count=0,
                    low_confidence_count=0,
                )
            rows = list(
                session.scalars(
                    select(MediaCoverageMovie).where(MediaCoverageMovie.run_id == run.id)
                )
            )
            buckets = {key: 0 for key in ("ZERO", "ONE", "TWO", "THREE_PLUS", "UNKNOWN")}
            for row in rows:
                buckets[row.bucket] = buckets.get(row.bucket, 0) + 1
            return CoverageSummaryView(
                generated_at=utc_now(),
                last_run=self._run_view(run),
                total_movies=len(rows),
                buckets=buckets,
                known_site_count=run.site_count,
                watched_torrent_count=run.watched_count,
                low_confidence_count=sum(1 for row in rows if row.confidence == "LOW"),
            )

    def list_movies(
        self,
        *,
        bucket: str | None = None,
        query: str | None = None,
        resolution: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> CoverageMovieListView:
        with self._session_factory() as session:
            run = self._latest_run(session)
            if run is None:
                return CoverageMovieListView(items=[], total=0, page=1, page_size=page_size)
            rows = list(
                session.scalars(
                    select(MediaCoverageMovie).where(MediaCoverageMovie.run_id == run.id)
                )
            )
        needle = (query or "").strip().casefold()
        items = [
            CoverageMovieView(
                movie_key=row.movie_key,
                display_title=row.display_title,
                year=row.year,
                resolution=row.resolution,
                site_keys=list(row.site_keys or []),
                coverage_count=row.coverage_count,
                bucket=row.bucket,
                match_level=row.match_level,
                confidence=row.confidence,
                suggestion=row.suggestion,
                relative_path_hint=row.relative_path_hint,
            )
            for row in rows
            if (not bucket or row.bucket == bucket)
            and (not needle or needle in row.display_title.casefold())
            and (not resolution or row.resolution == resolution)
        ]
        items.sort(key=lambda item: (-item.coverage_count, item.display_title))
        total = len(items)
        bounded_page = max(page, 1)
        bounded_size = min(max(page_size, 1), 200)
        offset = (bounded_page - 1) * bounded_size
        return CoverageMovieListView(
            items=items[offset : offset + bounded_size],
            total=total,
            page=bounded_page,
            page_size=bounded_size,
        )

    def export_csv(self, bucket: str | None = None) -> str:
        with self._session_factory() as session:
            run = self._latest_run(session)
            if run is None:
                return "\ufeff"
            rows = list(
                session.scalars(
                    select(MediaCoverageMovie).where(MediaCoverageMovie.run_id == run.id)
                )
            )
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "影片名称",
                "年份",
                "分辨率",
                "外部ID",
                "当前站点",
                "覆盖站数",
                "分桶",
                "建议",
                "匹配级别",
                "置信度",
                "安全相对路径",
            ]
        )
        for row in rows:
            if bucket and row.bucket != bucket:
                continue
            writer.writerow(
                [
                    row.display_title,
                    row.year or "",
                    row.resolution or "",
                    ";".join(
                        f"{item.get('namespace', '')}:{item.get('value', '')}"
                        for item in row.external_ids or []
                    ),
                    ";".join(row.site_keys or []),
                    row.coverage_count,
                    row.bucket,
                    row.suggestion,
                    row.match_level,
                    row.confidence,
                    row.relative_path_hint or "",
                ]
            )
        return "\ufeff" + stream.getvalue()
