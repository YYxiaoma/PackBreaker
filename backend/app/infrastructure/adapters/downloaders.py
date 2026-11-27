from __future__ import annotations

import re
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx2

from backend.app.domain.downloader import (
    ConnectionTestResult,
    DownloaderCapabilities,
    DownloaderCredential,
    DownloaderRuntimeMetrics,
    normalize_remote_path,
)
from backend.app.domain.verification import (
    EVIDENCE_VERIFIED_LEVELS,
    DownloaderKind,
    VerificationLevel,
)

_MAX_TORRENT_UPLOAD_BYTES = 20 * 1024 * 1024
_QBITTORRENT_JSON_ADD_API_MIN = (2, 14, 0)
_QBITTORRENT_SKIP_CHECKING_REMOVED = (2, 16, 0)
_STOPPED_STATES = frozenset({"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"})
_CHECKING_STATES = frozenset({"checkingDL", "checkingUP", "checkingResumeData"})
_STOPPED_UPLOAD_STATES = frozenset({"stoppedUP", "pausedUP"})
_STOPPED_DOWNLOAD_STATES = frozenset({"stoppedDL", "pausedDL"})
_SEEDING_STATES = frozenset({"uploading", "stalledUP", "queuedUP", "forcedUP"})
_TRANSMISSION_STOPPED = 0
_TRANSMISSION_CHECKING_STATES = frozenset({1, 2})
_TRANSMISSION_SEEDING_STATES = frozenset({5, 6})
TRACKER_STATE_OK = "OK"
TRACKER_STATE_ERROR = "ERROR"
TRACKER_STATE_UNKNOWN = "UNKNOWN"
_TRACKER_STATES = frozenset({TRACKER_STATE_OK, TRACKER_STATE_ERROR, TRACKER_STATE_UNKNOWN})
_QBITTORRENT_TRACKER_WORKING = 2
_QBITTORRENT_TRACKER_NOT_WORKING = 4


class DownloaderAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DownloaderProbeAdapter(Protocol):
    async def test_connection(self) -> ConnectionTestResult: ...

    async def runtime_metrics(self) -> DownloaderRuntimeMetrics: ...


@dataclass(frozen=True, slots=True)
class DownloaderTorrent:
    torrent_hash: str
    name: str
    status: str
    progress: float
    size_bytes: int
    category: str | None
    tags: tuple[str, ...]
    tracker: str | None
    save_path: str
    content_path: str | None
    # 做种健康巡查所需的只读观测字段；下载器未提供时保持 None，绝不猜测。
    seeding_time_seconds: int | None = None
    added_at: datetime | None = None
    tracker_state: str | None = None
    tracker_message: str | None = None
    tracker_last_announce_at: datetime | None = None
    # 同一任务常配置多个 tracker；站点归属要逐个尝试，不能只看第一个。
    tracker_urls: tuple[str, ...] = ()
    # 种子自带 comment，身份解析（站点详情页链接）直接读它，无需导出 .torrent。
    comment: str | None = None
    # 累计上传量是收益归因的唯一实测指标；客户端未提供时保持 None。
    uploaded_bytes: int | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 4096:
            raise ValueError("torrent 名称无效")
        if isinstance(self.progress, bool) or not isinstance(self.progress, (int, float)):
            raise ValueError("torrent progress 必须是 0..1 数值")
        if not 0.0 <= float(self.progress) <= 1.0:
            raise ValueError("torrent progress 必须位于 0..1")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError("torrent 大小必须是整数")
        if self.size_bytes < 0:
            raise ValueError("torrent 大小不能小于 0")
        if self.seeding_time_seconds is not None and (
            isinstance(self.seeding_time_seconds, bool)
            or not isinstance(self.seeding_time_seconds, int)
            or self.seeding_time_seconds < 0
        ):
            raise ValueError("torrent 做种时长必须是非负整数")
        if self.added_at is not None and not isinstance(self.added_at, datetime):
            raise ValueError("torrent 添加时间必须是 datetime")
        if self.tracker_state is not None and self.tracker_state not in _TRACKER_STATES:
            raise ValueError("torrent tracker 状态无效")
        if self.tracker_last_announce_at is not None and not isinstance(
            self.tracker_last_announce_at, datetime
        ):
            raise ValueError("torrent tracker 最后通告时间必须是 datetime")
        if self.comment is not None and not isinstance(self.comment, str):
            raise ValueError("torrent comment 必须是字符串")
        if any(not isinstance(item, str) or not item for item in self.tracker_urls):
            raise ValueError("torrent tracker 列表必须是非空字符串")


@dataclass(frozen=True, slots=True)
class DownloaderTrackerState:
    """单个种子的 tracker 观测结果。message 保留原始文案，供用户自行判断。"""

    state: str
    message: str | None = None
    last_announce_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state not in _TRACKER_STATES:
            raise ValueError("tracker 状态无效")


class DownloaderTorrentListAdapter(Protocol):
    async def list_torrents(self) -> tuple[DownloaderTorrent, ...]: ...


class DownloaderTrackerProbeAdapter(Protocol):
    async def get_tracker_states(
        self, torrent_hashes: tuple[str, ...]
    ) -> dict[str, DownloaderTrackerState]: ...


class DownloaderTorrentExportAdapter(Protocol):
    async def export_torrent(self, torrent_hash: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class QbittorrentAddRequest:
    torrent_content: bytes
    save_path: str
    verification_level: VerificationLevel
    skip_checking: bool = False
    tags: tuple[str, ...] = ()
    category: str | None = None
    paused: bool = True

    def __post_init__(self) -> None:
        if not self.torrent_content or len(self.torrent_content) > _MAX_TORRENT_UPLOAD_BYTES:
            raise ValueError("qBittorrent torrent payload 长度无效")
        object.__setattr__(self, "save_path", normalize_remote_path(self.save_path))
        if not self.paused:
            raise ValueError("PackBreaker 安全添加必须以暂停状态创建 qBittorrent 任务")
        # 跳过校验要求“已有内容证据等级”：逐片全量（FULL_VERIFIED）或
        # 按确定性规则抽样（SAMPLED_VERIFIED）。CLIENT_CHECK_REQUIRED / BLOCKED
        # 仍然禁止跳过，交给下载器做全量校验。
        if self.skip_checking and self.verification_level not in EVIDENCE_VERIFIED_LEVELS:
            raise ValueError("只有具备内容证据等级的 torrent 才允许 qBittorrent skip_checking")
        normalized_tags = tuple(dict.fromkeys(tag.strip() for tag in self.tags if tag.strip()))
        if any(len(tag) > 128 or "," in tag for tag in normalized_tags):
            raise ValueError("qBittorrent tag 格式无效")
        object.__setattr__(self, "tags", normalized_tags)
        if self.category is not None:
            category = self.category.strip()
            if not category or len(category) > 128:
                raise ValueError("qBittorrent category 格式无效")
            object.__setattr__(self, "category", category)


@dataclass(frozen=True, slots=True)
class QbittorrentAddResult:
    success_count: int
    pending_count: int
    failure_count: int
    added_torrent_ids: tuple[str, ...]
    webapi_version: str


@dataclass(frozen=True, slots=True)
class QbittorrentTorrentState:
    torrent_hash: str
    save_path: str
    content_path: str | None
    state: str
    tags: tuple[str, ...]
    progress: float

    def __post_init__(self) -> None:
        if isinstance(self.progress, bool) or not isinstance(self.progress, (int, float)):
            raise ValueError("qBittorrent progress 必须是 0..1 数值")
        normalized = float(self.progress)
        if not 0.0 <= normalized <= 1.0:
            raise ValueError("qBittorrent progress 必须位于 0..1")
        object.__setattr__(self, "progress", normalized)

    @property
    def stopped(self) -> bool:
        return self.state in _STOPPED_STATES

    @property
    def checking(self) -> bool:
        return self.state in _CHECKING_STATES

    @property
    def verification_complete(self) -> bool:
        return self.state in _STOPPED_UPLOAD_STATES and self.progress == 1.0

    @property
    def verification_incomplete(self) -> bool:
        return self.state in _STOPPED_DOWNLOAD_STATES and self.progress < 1.0

    @property
    def seeding(self) -> bool:
        return self.state in _SEEDING_STATES and self.progress == 1.0


class DownloaderFileSelectionAdapter(Protocol):
    async def set_file_selection(
        self,
        torrent_hash: str,
        *,
        wanted: tuple[int, ...],
        unwanted: tuple[int, ...],
    ) -> None: ...


class QbittorrentWriteAdapter(Protocol):
    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult: ...

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[QbittorrentTorrentState, ...]: ...

    async def stop_torrent(self, torrent_hash: str) -> None: ...

    async def start_torrent(self, torrent_hash: str) -> None: ...

    async def recheck_torrent(self, torrent_hash: str) -> None: ...

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TransmissionAddRequest:
    torrent_content: bytes
    save_path: str
    labels: tuple[str, ...] = ()
    paused: bool = True

    def __post_init__(self) -> None:
        if not self.torrent_content or len(self.torrent_content) > _MAX_TORRENT_UPLOAD_BYTES:
            raise ValueError("Transmission torrent payload 长度无效")
        object.__setattr__(self, "save_path", normalize_remote_path(self.save_path))
        if not self.paused:
            raise ValueError("PackBreaker 安全添加必须以暂停状态创建 Transmission 任务")
        labels = tuple(dict.fromkeys(item.strip() for item in self.labels if item.strip()))
        if any(len(item) > 128 or "\x00" in item for item in labels):
            raise ValueError("Transmission label 格式无效")
        object.__setattr__(self, "labels", labels)


@dataclass(frozen=True, slots=True)
class TransmissionAddResult:
    torrent_hash: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class TransmissionTorrentState:
    torrent_hash: str
    download_dir: str
    status: int
    labels: tuple[str, ...]
    percent_done: float
    recheck_progress: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.status, bool)
            or not isinstance(self.status, int)
            or not 0 <= self.status <= 6
        ):
            raise ValueError("Transmission status 必须位于 0..6")
        for field_name in ("percent_done", "recheck_progress"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Transmission {field_name} 必须是 0..1 数值")
            normalized = float(value)
            if not 0.0 <= normalized <= 1.0:
                raise ValueError(f"Transmission {field_name} 必须位于 0..1")
            object.__setattr__(self, field_name, normalized)

    @property
    def stopped(self) -> bool:
        return self.status == _TRANSMISSION_STOPPED

    @property
    def checking(self) -> bool:
        return self.status in _TRANSMISSION_CHECKING_STATES

    @property
    def verification_complete(self) -> bool:
        return self.stopped and self.percent_done == 1.0

    @property
    def verification_incomplete(self) -> bool:
        return self.stopped and self.percent_done < 1.0

    @property
    def seeding(self) -> bool:
        return self.status in _TRANSMISSION_SEEDING_STATES and self.percent_done == 1.0


class TransmissionWriteAdapter(Protocol):
    async def add_torrent(self, request: TransmissionAddRequest) -> TransmissionAddResult: ...

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[TransmissionTorrentState, ...]: ...

    async def stop_torrent(self, torrent_hash: str) -> None: ...

    async def start_torrent(self, torrent_hash: str) -> None: ...

    async def verify_torrent(self, torrent_hash: str) -> None: ...

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None: ...


class DownloaderAdapterFactory:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def create(
        self,
        *,
        kind: DownloaderKind,
        base_url: str,
        credential: DownloaderCredential | None,
    ) -> DownloaderProbeAdapter:
        if kind is DownloaderKind.QBITTORRENT:
            return QbittorrentAdapter(base_url, credential, transport=self._transport)
        return TransmissionAdapter(base_url, credential, transport=self._transport)

    def create_qbittorrent(
        self,
        *,
        base_url: str,
        credential: DownloaderCredential | None,
    ) -> QbittorrentAdapter:
        return QbittorrentAdapter(base_url, credential, transport=self._transport)

    def create_transmission(
        self,
        *,
        base_url: str,
        credential: DownloaderCredential | None,
    ) -> TransmissionAdapter:
        return TransmissionAdapter(base_url, credential, transport=self._transport)


class QbittorrentAdapter:
    def __init__(
        self,
        base_url: str,
        credential: DownloaderCredential | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._transport = transport

    async def test_connection(self) -> ConnectionTestResult:
        async with self._authenticated_client() as client:
            version_response = await client.get(f"{self._base_url}/api/v2/app/version")
            webapi_response = await client.get(f"{self._base_url}/api/v2/app/webapiVersion")
            self._raise_for_probe_status(version_response)
            self._raise_for_probe_status(webapi_response)
            version = self._validated_text(version_response.text, "qBittorrent 版本")
            webapi_version = self._validated_text(webapi_response.text, "WebAPI 版本")
            parsed_webapi_version = _parse_api_version(webapi_version)
        return ConnectionTestResult(
            DownloaderCapabilities(
                client="qBittorrent",
                version=version,
                api_version=webapi_version,
                supports_skip_checking=(parsed_webapi_version < _QBITTORRENT_SKIP_CHECKING_REMOVED),
                supports_force_recheck=True,
                supports_verify_progress=True,
                supports_selective_files=True,
            )
        )

    async def runtime_metrics(self) -> DownloaderRuntimeMetrics:
        async with self._authenticated_client() as client:
            transfer_response = await client.get(f"{self._base_url}/api/v2/transfer/info")
            self._raise_for_probe_status(transfer_response)
            torrents_response = await client.get(f"{self._base_url}/api/v2/torrents/info")
            self._raise_for_probe_status(torrents_response)
            sync_response = await client.get(
                f"{self._base_url}/api/v2/sync/maindata", params={"rid": 0}
            )
            self._raise_for_probe_status(sync_response)
            try:
                transfer = transfer_response.json()
                torrents = torrents_response.json()
                sync_data = sync_response.json()
            except ValueError as exc:
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent 运行指标响应无法解析"
                ) from exc
        if (
            not isinstance(transfer, dict)
            or not isinstance(torrents, list)
            or not isinstance(sync_data, dict)
        ):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "qBittorrent 运行指标响应格式无效"
            )
        upload_speed = _required_nonnegative_int(
            transfer.get("up_info_speed"), "qBittorrent 上传速度"
        )
        download_speed = _required_nonnegative_int(
            transfer.get("dl_info_speed"), "qBittorrent 下载速度"
        )
        total_size = 0
        for raw in torrents:
            if not isinstance(raw, dict):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 指标项格式无效"
                )
            total_size += _required_nonnegative_int(raw.get("size"), "qBittorrent torrent 大小")
        free_space: int | None = None
        server_state = sync_data.get("server_state")
        if isinstance(server_state, dict):
            raw_free_space = server_state.get("free_space_on_disk")
            if isinstance(raw_free_space, int) and not isinstance(raw_free_space, bool):
                free_space = raw_free_space if raw_free_space >= 0 else None
            elif raw_free_space is not None:
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent 剩余空间字段无效"
                )
        return DownloaderRuntimeMetrics(
            upload_speed_bytes_per_second=upload_speed,
            download_speed_bytes_per_second=download_speed,
            total_content_size_bytes=total_size,
            free_space_bytes=free_space,
            active_torrent_count=None,
            total_torrent_count=len(torrents),
            sampled_at=datetime.now(UTC),
        )

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        if request.skip_checking and request.verification_level not in EVIDENCE_VERIFIED_LEVELS:
            raise DownloaderAdapterError(
                "DOWNLOADER_SKIP_CHECKING_BLOCKED",
                "只有具备内容证据等级的 torrent 才允许 qBittorrent skip_checking",
            )
        async with self._authenticated_client() as client:
            webapi_response = await client.get(f"{self._base_url}/api/v2/app/webapiVersion")
            self._raise_for_probe_status(webapi_response)
            webapi_version = self._validated_text(webapi_response.text, "WebAPI 版本")
            parsed_version = _parse_api_version(webapi_version)
            if parsed_version < _QBITTORRENT_JSON_ADD_API_MIN:
                raise DownloaderAdapterError(
                    "DOWNLOADER_API_UNSUPPORTED",
                    "qBittorrent WebAPI 版本过旧，不支持受控 JSON 添加结果",
                )
            if parsed_version >= _QBITTORRENT_SKIP_CHECKING_REMOVED:
                raise DownloaderAdapterError(
                    "DOWNLOADER_API_UNSUPPORTED",
                    "当前 qBittorrent WebAPI 已改变添加协议，需要显式适配后才能写入",
                )

            form: dict[str, str] = {
                "savepath": request.save_path,
                "paused": "true",
                # qBittorrent 5.x uses `stopped`; retaining `paused` alone can
                # silently start a new torrent on WebAPI 2.15.x. Both are safe
                # and the older alias is kept for the supported legacy API.
                "stopped": "true",
                "skip_checking": "true" if request.skip_checking else "false",
            }
            if request.tags:
                form["tags"] = ",".join(request.tags)
            if request.category is not None:
                form["category"] = request.category
            response = await client.post(
                f"{self._base_url}/api/v2/torrents/add",
                data=form,
                files={
                    "torrents": (
                        "packbreaker.torrent",
                        request.torrent_content,
                        "application/x-bittorrent",
                    )
                },
            )
            if response.status_code in {401, 403}:
                raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败")
            if response.status_code == 409:
                raise DownloaderAdapterError(
                    "DOWNLOADER_ADD_REJECTED", "qBittorrent 拒绝添加 torrent"
                )
            if response.status_code not in {200, 202}:
                raise DownloaderAdapterError(
                    "DOWNLOADER_ADD_FAILED", "qBittorrent 添加接口返回异常状态"
                )
            return _parse_add_result(response, webapi_version)

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[QbittorrentTorrentState, ...]:
        normalized = tuple(dict.fromkeys(_normalize_torrent_hash(item) for item in torrent_hashes))
        if not normalized:
            raise ValueError("至少需要一个 qBittorrent torrent hash")
        async with self._authenticated_client() as client:
            response = await client.get(
                f"{self._base_url}/api/v2/torrents/info",
                params={"hashes": "|".join(normalized)},
            )
            self._raise_for_probe_status(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 状态响应无法解析"
                ) from exc
            if not isinstance(payload, list):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 状态响应格式无效"
                )
            states: list[QbittorrentTorrentState] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 状态项格式无效"
                    )
                torrent_hash = raw.get("hash")
                save_path = raw.get("save_path")
                state = raw.get("state")
                content_path = raw.get("content_path")
                tags = raw.get("tags", "")
                progress = raw.get("progress")
                if (
                    not isinstance(torrent_hash, str)
                    or not isinstance(save_path, str)
                    or not isinstance(state, str)
                    or (content_path is not None and not isinstance(content_path, str))
                    or not isinstance(tags, str)
                    or isinstance(progress, bool)
                    or not isinstance(progress, (int, float))
                    or not 0.0 <= float(progress) <= 1.0
                ):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 状态字段无效"
                    )
                try:
                    normalized_save_path = normalize_remote_path(save_path)
                except (ValueError, TypeError) as exc:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent save path 格式无效"
                    ) from exc
                states.append(
                    QbittorrentTorrentState(
                        torrent_hash=_normalize_torrent_hash(torrent_hash),
                        save_path=normalized_save_path,
                        content_path=content_path,
                        state=state,
                        tags=tuple(tag.strip() for tag in tags.split(",") if tag.strip()),
                        progress=float(progress),
                    )
                )
            return tuple(states)

    async def list_torrents(self) -> tuple[DownloaderTorrent, ...]:
        async with self._authenticated_client() as client:
            response = await client.get(f"{self._base_url}/api/v2/torrents/info")
            self._raise_for_probe_status(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 列表响应无法解析"
                ) from exc
            if not isinstance(payload, list):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 列表响应格式无效"
                )
            items: list[DownloaderTorrent] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 列表项格式无效"
                    )
                torrent_hash = raw.get("hash")
                name = raw.get("name")
                state = raw.get("state")
                progress = raw.get("progress")
                size = raw.get("size")
                category = raw.get("category", "")
                tags = raw.get("tags", "")
                tracker = raw.get("tracker", "")
                save_path = raw.get("save_path")
                content_path = raw.get("content_path")
                seeding_time = raw.get("seeding_time")
                added_on = raw.get("added_on")
                comment = raw.get("comment")
                uploaded = raw.get("uploaded")
                if (
                    not isinstance(torrent_hash, str)
                    or not isinstance(name, str)
                    or not isinstance(state, str)
                    or isinstance(progress, bool)
                    or not isinstance(progress, (int, float))
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or not isinstance(category, str)
                    or not isinstance(tags, str)
                    or not isinstance(tracker, str)
                    or not isinstance(save_path, str)
                    or (content_path is not None and not isinstance(content_path, str))
                ):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 列表字段无效"
                    )
                try:
                    items.append(
                        DownloaderTorrent(
                            torrent_hash=_normalize_torrent_hash(torrent_hash),
                            name=name,
                            status=state,
                            progress=float(progress),
                            size_bytes=size,
                            category=category.strip() or None,
                            tags=tuple(tag.strip() for tag in tags.split(",") if tag.strip()),
                            tracker=tracker.strip() or None,
                            tracker_urls=(tracker.strip(),) if tracker.strip() else (),
                            save_path=normalize_remote_path(save_path),
                            content_path=(
                                normalize_remote_path(content_path) if content_path else None
                            ),
                            seeding_time_seconds=_optional_seeding_seconds(seeding_time),
                            added_at=_optional_unix_datetime(added_on),
                            comment=comment if isinstance(comment, str) else None,
                            uploaded_bytes=_optional_nonnegative_int(uploaded, "uploaded"),
                        )
                    )
                except (ValueError, TypeError) as exc:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent torrent 列表字段无效"
                    ) from exc
            return tuple(items)

    async def get_tracker_states(
        self, torrent_hashes: tuple[str, ...]
    ) -> dict[str, DownloaderTrackerState]:
        """Read tracker status per hash. qBittorrent exposes no batch form.

        Callers decide the sampling policy; this method performs exactly one
        request per hash and never retries.
        """
        normalized = tuple(dict.fromkeys(_normalize_torrent_hash(item) for item in torrent_hashes))
        if not normalized:
            return {}
        states: dict[str, DownloaderTrackerState] = {}
        async with self._authenticated_client() as client:
            for torrent_hash in normalized:
                response = await client.get(
                    f"{self._base_url}/api/v2/torrents/trackers",
                    params={"hash": torrent_hash},
                )
                self._raise_for_probe_status(response)
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent tracker 列表响应无法解析"
                    ) from exc
                if not isinstance(payload, list):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "qBittorrent tracker 列表响应格式无效"
                    )
                states[torrent_hash] = _qbittorrent_tracker_state(payload)
        return states

    async def export_torrent(self, torrent_hash: str) -> bytes:
        """Export the stored .torrent; used once to resolve site identity."""
        normalized = _normalize_torrent_hash(torrent_hash)
        async with self._authenticated_client() as client:
            response = await client.get(
                f"{self._base_url}/api/v2/torrents/export",
                params={"hash": normalized},
            )
            self._raise_for_probe_status(response)
            content = response.content
        if not content or content[:1] != b"d":
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "qBittorrent 导出的 torrent 内容无效"
            )
        return content

    async def stop_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("stop", torrent_hash)

    async def start_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("start", torrent_hash)

    async def recheck_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("recheck", torrent_hash)

    async def set_file_selection(
        self,
        torrent_hash: str,
        *,
        wanted: tuple[int, ...],
        unwanted: tuple[int, ...],
    ) -> None:
        normalized = _normalize_torrent_hash(torrent_hash)
        wanted_ids, unwanted_ids = _normalize_file_selection(wanted, unwanted)
        async with self._authenticated_client() as client:
            for file_ids, priority in ((unwanted_ids, 0), (wanted_ids, 1)):
                if not file_ids:
                    continue
                response = await client.post(
                    f"{self._base_url}/api/v2/torrents/filePrio",
                    data={
                        "hash": normalized,
                        "id": "|".join(str(item) for item in file_ids),
                        "priority": str(priority),
                    },
                )
                if response.status_code in {401, 403}:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_AUTH_FAILED",
                        "qBittorrent 认证失败",
                    )
                if response.status_code not in {200, 204}:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_WRITE_FAILED",
                        "qBittorrent 文件选择设置失败",
                    )

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        normalized = _normalize_torrent_hash(torrent_hash)
        async with self._authenticated_client() as client:
            response = await client.post(
                f"{self._base_url}/api/v2/torrents/delete",
                data={"hashes": normalized, "deleteFiles": "false"},
            )
            if response.status_code in {401, 403}:
                raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败")
            if response.status_code not in {200, 204}:
                raise DownloaderAdapterError(
                    "DOWNLOADER_WRITE_FAILED",
                    "qBittorrent 移除 torrent 任务失败",
                )

    async def _torrent_action(self, action: str, torrent_hash: str) -> None:
        normalized = _normalize_torrent_hash(torrent_hash)
        async with self._authenticated_client() as client:
            response = await client.post(
                f"{self._base_url}/api/v2/torrents/{action}",
                data={"hashes": normalized},
            )
            if response.status_code in {401, 403}:
                raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败")
            if response.status_code not in {200, 204}:
                raise DownloaderAdapterError(
                    "DOWNLOADER_WRITE_FAILED", "qBittorrent torrent 状态操作失败"
                )

    @asynccontextmanager
    async def _authenticated_client(self) -> AsyncIterator[httpx2.AsyncClient]:
        headers: dict[str, str] = {}
        if self._credential is not None and self._credential.api_key:
            headers["Authorization"] = f"Bearer {self._credential.api_key}"
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                if self._credential is not None and self._credential.username:
                    parsed = urlsplit(self._base_url)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    login = await client.post(
                        f"{self._base_url}/api/v2/auth/login",
                        data={
                            "username": self._credential.username,
                            "password": self._credential.password or "",
                        },
                        headers={"Origin": origin, "Referer": f"{origin}/"},
                    )
                    legacy_login_ok = login.status_code == 200 and login.text.strip().startswith(
                        "Ok."
                    )
                    session_login_ok = login.status_code == 204 and any(
                        name in {"SID", "QBT_SID"} or name.startswith("QBT_SID_")
                        for name in client.cookies
                    )
                    if not legacy_login_ok and not session_login_ok:
                        raise DownloaderAdapterError(
                            "DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败"
                        )
                yield client
        except DownloaderAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "qBittorrent 连接失败或超时"
            ) from exc
        except httpx2.HTTPError as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_CONNECTION_FAILED", "qBittorrent HTTP 调用失败"
            ) from exc

    @staticmethod
    def _raise_for_probe_status(response: httpx2.Response) -> None:
        if response.status_code in {401, 403}:
            raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败")
        if response.status_code != 200:
            raise DownloaderAdapterError(
                "DOWNLOADER_CONNECTION_FAILED", "qBittorrent API 返回异常状态"
            )

    @staticmethod
    def _validated_text(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", f"{label}响应格式无效")
        return normalized


def _parse_add_result(response: httpx2.Response, webapi_version: str) -> QbittorrentAddResult:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "qBittorrent 添加响应无法解析"
        ) from exc
    if not isinstance(payload, dict):
        raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", "qBittorrent 添加响应格式无效")
    success_count = payload.get("success_count")
    pending_count = payload.get("pending_count")
    failure_count = payload.get("failure_count")
    added_ids = payload.get("added_torrent_ids")
    if (
        not isinstance(success_count, int)
        or isinstance(success_count, bool)
        or not isinstance(pending_count, int)
        or isinstance(pending_count, bool)
        or not isinstance(failure_count, int)
        or isinstance(failure_count, bool)
        or min(success_count, pending_count, failure_count) < 0
        or not isinstance(added_ids, list)
        or not all(isinstance(item, str) for item in added_ids)
    ):
        raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", "qBittorrent 添加响应字段无效")
    normalized_ids = tuple(_normalize_torrent_hash(item) for item in added_ids)
    if success_count + pending_count + failure_count != 1:
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "qBittorrent 单 torrent 添加计数不一致"
        )
    return QbittorrentAddResult(
        success_count=success_count,
        pending_count=pending_count,
        failure_count=failure_count,
        added_torrent_ids=normalized_ids,
        webapi_version=webapi_version,
    )


def _normalize_torrent_hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", "下载器 torrent hash 格式无效")
    return normalized


def _parse_api_version(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) not in {2, 3}:
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "qBittorrent WebAPI 版本格式无效"
        )
    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "qBittorrent WebAPI 版本格式无效"
        ) from exc
    if any(number < 0 for number in numbers):
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "qBittorrent WebAPI 版本格式无效"
        )
    return (numbers[0], numbers[1], numbers[2] if len(numbers) == 3 else 0)


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", f"{label}字段无效")
    return value


def _required_nonnegative_int(value: object, label: str) -> int:
    parsed = _optional_nonnegative_int(value, label)
    if parsed is None:
        raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", f"{label}字段缺失")
    return parsed


def _optional_unknown_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", f"{label}字段无效")
    return value if value >= 0 else None


class TransmissionAdapter:
    def __init__(
        self,
        base_url: str,
        credential: DownloaderCredential | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._credential = credential
        self._transport = transport
        # None=尚未探测；"jsonrpc2"=Transmission 4.1+；"legacy"=4.0.x 及更早的旧信封。
        self._dialect: str | None = None

    async def test_connection(self) -> ConnectionTestResult:
        result, rpc_header = await self._rpc(
            "session_get",
            {"fields": ["version", "rpc_version_semver"]},
        )
        version = result.get("version")
        if not isinstance(version, str) or not version or len(version) > 128:
            raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", "Transmission 版本响应无效")
        rpc_version = result.get("rpc_version_semver")
        if rpc_version is None:
            rpc_version = rpc_header
        if rpc_version is not None and not isinstance(rpc_version, str):
            rpc_version = str(rpc_version)
        return ConnectionTestResult(
            DownloaderCapabilities(
                client="Transmission",
                version=version,
                api_version=rpc_version,
                supports_skip_checking=False,
                supports_force_recheck=True,
                supports_verify_progress=True,
                supports_selective_files=True,
            )
        )

    async def runtime_metrics(self) -> DownloaderRuntimeMetrics:
        stats, _ = await self._rpc("session_stats", {})
        session, _ = await self._rpc(
            "session_get",
            {"fields": ["download_dir", "download_dir_free_space"]},
        )
        torrents, _ = await self._rpc("torrent_get", {"fields": ["total_size"]})
        download_dir = session.get("download_dir")
        if not isinstance(download_dir, str) or not download_dir.strip():
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission 下载目录响应无效"
            )
        free_space = _optional_unknown_nonnegative_int(
            session.get("download_dir_free_space"),
            "Transmission 兼容剩余空间",
        )
        try:
            free_space_result, _ = await self._rpc("free_space", {"path": download_dir})
        except DownloaderAdapterError as exc:
            if exc.code != "DOWNLOADER_WRITE_FAILED":
                raise
        else:
            free_space = _optional_unknown_nonnegative_int(
                free_space_result.get("size_bytes"),
                "Transmission 剩余空间",
            )
        raw_torrents = torrents.get("torrents")
        if not isinstance(raw_torrents, list):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 指标响应格式无效"
            )
        total_size = 0
        for raw in raw_torrents:
            if not isinstance(raw, dict):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 指标项格式无效"
                )
            total_size += _required_nonnegative_int(
                raw.get("total_size"), "Transmission torrent 大小"
            )
        return DownloaderRuntimeMetrics(
            upload_speed_bytes_per_second=_required_nonnegative_int(
                stats.get("upload_speed"), "Transmission 上传速度"
            ),
            download_speed_bytes_per_second=_required_nonnegative_int(
                stats.get("download_speed"), "Transmission 下载速度"
            ),
            total_content_size_bytes=total_size,
            free_space_bytes=free_space,
            active_torrent_count=_required_nonnegative_int(
                stats.get("active_torrent_count"), "Transmission 活动任务数"
            ),
            total_torrent_count=_required_nonnegative_int(
                stats.get("torrent_count"), "Transmission 任务总数"
            ),
            sampled_at=datetime.now(UTC),
        )

    async def add_torrent(self, request: TransmissionAddRequest) -> TransmissionAddResult:
        result, _ = await self._rpc(
            "torrent_add",
            {
                "metainfo": b64encode(request.torrent_content).decode("ascii"),
                "download_dir": request.save_path,
                "paused": True,
                "labels": list(request.labels),
            },
        )
        added = result.get("torrent_added")
        duplicate = result.get("torrent_duplicate")
        if (added is None) == (duplicate is None):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE",
                "Transmission 添加响应缺少唯一 torrent 结果",
            )
        item = added if added is not None else duplicate
        if not isinstance(item, dict):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission 添加响应格式无效"
            )
        torrent_hash = item.get("hash_string")
        if not isinstance(torrent_hash, str):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission 添加响应 torrent hash 无效"
            )
        return TransmissionAddResult(
            torrent_hash=_normalize_torrent_hash(torrent_hash),
            duplicate=duplicate is not None,
        )

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[TransmissionTorrentState, ...]:
        normalized = tuple(dict.fromkeys(_normalize_torrent_hash(item) for item in torrent_hashes))
        if not normalized:
            raise ValueError("至少需要一个 Transmission torrent hash")
        result, _ = await self._rpc(
            "torrent_get",
            {
                "ids": list(normalized),
                "fields": [
                    "hash_string",
                    "download_dir",
                    "status",
                    "labels",
                    "percent_done",
                    "recheck_progress",
                ],
            },
        )
        raw_torrents = result.get("torrents")
        if not isinstance(raw_torrents, list):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 状态响应格式无效"
            )
        states: list[TransmissionTorrentState] = []
        for raw in raw_torrents:
            if not isinstance(raw, dict):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 状态项格式无效"
                )
            torrent_hash = raw.get("hash_string")
            download_dir = raw.get("download_dir")
            status = raw.get("status")
            labels = raw.get("labels")
            percent_done = raw.get("percent_done")
            recheck_progress = raw.get("recheck_progress")
            if (
                not isinstance(torrent_hash, str)
                or not isinstance(download_dir, str)
                or isinstance(status, bool)
                or not isinstance(status, int)
                or not isinstance(labels, list)
                or not all(isinstance(item, str) for item in labels)
                or isinstance(percent_done, bool)
                or not isinstance(percent_done, (int, float))
                or isinstance(recheck_progress, bool)
                or not isinstance(recheck_progress, (int, float))
            ):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 状态字段无效"
                )
            try:
                state = TransmissionTorrentState(
                    torrent_hash=_normalize_torrent_hash(torrent_hash),
                    download_dir=normalize_remote_path(download_dir),
                    status=status,
                    labels=tuple(item.strip() for item in labels if item.strip()),
                    percent_done=float(percent_done),
                    recheck_progress=float(recheck_progress),
                )
            except (ValueError, TypeError) as exc:
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 状态字段无效"
                ) from exc
            states.append(state)
        return tuple(states)

    async def list_torrents(self) -> tuple[DownloaderTorrent, ...]:
        result, _ = await self._rpc(
            "torrent_get",
            {
                "fields": [
                    "hash_string",
                    "name",
                    "download_dir",
                    "status",
                    "labels",
                    "percent_done",
                    "total_size",
                    "secondsSeeding",
                    "addedDate",
                    "trackerStats",
                    "trackers",
                    "comment",
                    # Transmission 4.1.3 实际返回 snake_case；旧版本/第三方实现可能用 camelCase。
                    "uploaded_ever",
                    "uploadedEver",
                ]
            },
        )
        raw_torrents = result.get("torrents")
        if not isinstance(raw_torrents, list):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 列表响应格式无效"
            )
        items: list[DownloaderTorrent] = []
        for raw in raw_torrents:
            if not isinstance(raw, dict):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 列表项格式无效"
                )
            torrent_hash = raw.get("hash_string")
            name = raw.get("name")
            download_dir = raw.get("download_dir")
            status = raw.get("status")
            labels = raw.get("labels")
            percent_done = raw.get("percent_done")
            total_size = raw.get("total_size")
            seconds_seeding = raw.get("secondsSeeding")
            added_date = raw.get("addedDate")
            tracker_stats = raw.get("trackerStats")
            trackers = raw.get("trackers")
            tracker_urls: list[str] = []
            if isinstance(trackers, list):
                for tracker in trackers:
                    if not isinstance(tracker, dict):
                        continue
                    announce = tracker.get("announce")
                    if isinstance(announce, str) and announce.strip():
                        url = announce.strip()
                        if url not in tracker_urls:
                            tracker_urls.append(url)
            tracker_url: str | None = tracker_urls[0] if tracker_urls else None
            comment = raw.get("comment")
            uploaded_ever = raw.get("uploaded_ever", raw.get("uploadedEver"))
            if (
                not isinstance(torrent_hash, str)
                or not isinstance(name, str)
                or not isinstance(download_dir, str)
                or isinstance(status, bool)
                or not isinstance(status, int)
                or not isinstance(labels, list)
                or not all(isinstance(item, str) for item in labels)
                or isinstance(percent_done, bool)
                or not isinstance(percent_done, (int, float))
                or isinstance(total_size, bool)
                or not isinstance(total_size, int)
            ):
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 列表字段无效"
                )
            tracker_state, tracker_message, last_announce_at = _transmission_tracker_state(
                tracker_stats
            )
            try:
                normalized_dir = normalize_remote_path(download_dir)
                items.append(
                    DownloaderTorrent(
                        torrent_hash=_normalize_torrent_hash(torrent_hash),
                        name=name,
                        status=str(status),
                        progress=float(percent_done),
                        size_bytes=total_size,
                        category=None,
                        tags=tuple(item.strip() for item in labels if item.strip()),
                        tracker=tracker_url,
                        tracker_urls=tuple(tracker_urls),
                        save_path=normalized_dir,
                        content_path=_remote_child(normalized_dir, name),
                        seeding_time_seconds=_optional_seeding_seconds(seconds_seeding),
                        added_at=_optional_unix_datetime(added_date),
                        tracker_state=tracker_state,
                        tracker_message=tracker_message,
                        tracker_last_announce_at=last_announce_at,
                        comment=comment if isinstance(comment, str) else None,
                        uploaded_bytes=_optional_nonnegative_int(uploaded_ever, "uploadedEver"),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 列表字段无效"
                ) from exc
        return tuple(items)

    async def get_tracker_states(
        self, torrent_hashes: tuple[str, ...]
    ) -> dict[str, DownloaderTrackerState]:
        """Re-read tracker stats for explicit hash sets.

        The list phase already returns tracker_state, so this exists for
        samplers that need a fresh value without a full list refresh.
        """
        normalized = tuple(dict.fromkeys(_normalize_torrent_hash(item) for item in torrent_hashes))
        if not normalized:
            return {}
        result, _ = await self._rpc(
            "torrent_get",
            {"ids": list(normalized), "fields": ["hash_string", "trackerStats"]},
        )
        raw_torrents = result.get("torrents")
        if not isinstance(raw_torrents, list):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission tracker 响应格式无效"
            )
        states: dict[str, DownloaderTrackerState] = {}
        for raw in raw_torrents:
            if not isinstance(raw, dict):
                continue
            torrent_hash = raw.get("hash_string")
            if not isinstance(torrent_hash, str):
                continue
            state, message, last_announce_at = _transmission_tracker_state(raw.get("trackerStats"))
            states[_normalize_torrent_hash(torrent_hash)] = DownloaderTrackerState(
                state=state, message=message, last_announce_at=last_announce_at
            )
        return states

    async def export_torrent(self, torrent_hash: str) -> bytes:
        normalized = _normalize_torrent_hash(torrent_hash)
        result, _ = await self._rpc(
            "torrent_get",
            {"ids": [normalized], "fields": ["hash_string", "torrentFile"]},
        )
        raw_torrents = result.get("torrents")
        if not isinstance(raw_torrents, list) or not raw_torrents:
            raise DownloaderAdapterError(
                "DOWNLOADER_TORRENT_NOT_FOUND", "Transmission 中找不到该种子"
            )
        raw = raw_torrents[0]
        encoded = raw.get("torrentFile") if isinstance(raw, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission 未返回 torrent 内容"
            )
        try:
            content = b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission torrent 内容无法解码"
            ) from exc
        if content[:1] != b"d":
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission 导出的 torrent 内容无效"
            )
        return content

    async def stop_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("torrent_stop", torrent_hash)

    async def start_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("torrent_start", torrent_hash)

    async def verify_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("torrent_verify", torrent_hash)

    async def set_file_selection(
        self,
        torrent_hash: str,
        *,
        wanted: tuple[int, ...],
        unwanted: tuple[int, ...],
    ) -> None:
        wanted_ids, unwanted_ids = _normalize_file_selection(wanted, unwanted)
        await self._rpc(
            "torrent_set",
            {
                "ids": [_normalize_torrent_hash(torrent_hash)],
                "files_wanted": list(wanted_ids),
                "files_unwanted": list(unwanted_ids),
            },
        )

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        await self._rpc(
            "torrent_remove",
            {"ids": [_normalize_torrent_hash(torrent_hash)], "delete_local_data": False},
        )

    async def _torrent_action(self, method: str, torrent_hash: str) -> None:
        await self._rpc(method, {"ids": [_normalize_torrent_hash(torrent_hash)]})

    async def _rpc(
        self,
        method: str,
        params: dict[str, object],
    ) -> tuple[dict[str, object], str | None]:
        """按协商出的方言发一次 RPC。

        Transmission 4.1 起改用 JSON-RPC 2.0（snake_case、params/result/id）；
        4.0.x 及更早仍是旧信封（kebab-case 方法名、arguments/tag、camelCase 字段）。
        首次调用探测一次方言，之后固定下来，避免每次请求都多打一轮。
        """

        if self._dialect in (None, "jsonrpc2"):
            body, rpc_header = await self._post(_jsonrpc2_payload(method, params))
            if _is_jsonrpc2_envelope(body):
                self._dialect = "jsonrpc2"
                return _jsonrpc2_result(body), rpc_header
            if self._dialect == "jsonrpc2":
                raise DownloaderAdapterError(
                    "DOWNLOADER_INVALID_RESPONSE", "Transmission JSON-RPC 响应标识无效"
                )
            # 旧协议不认 JSON-RPC 2.0 信封：换成旧信封重发同一次调用。
            self._dialect = "legacy"

        body, rpc_header = await self._post(_legacy_payload(method, params))
        if not _is_legacy_envelope(body):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 响应标识无效"
            )
        return _legacy_result(body), rpc_header

    async def _post(
        self,
        payload: dict[str, object],
    ) -> tuple[dict[str, object], str | None]:
        auth: httpx2.Auth | None = None
        if self._credential is not None and self._credential.username:
            auth = httpx2.BasicAuth(
                self._credential.username,
                self._credential.password or "",
            )
        try:
            async with httpx2.AsyncClient(
                auth=auth,
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(self._base_url, json=payload)
                rpc_header = response.headers.get("X-Transmission-Rpc-Version")
                if response.status_code == 409:
                    session_id = response.headers.get("X-Transmission-Session-Id")
                    if not session_id:
                        raise DownloaderAdapterError(
                            "DOWNLOADER_INVALID_RESPONSE",
                            "Transmission 缺少 RPC session id",
                        )
                    response = await client.post(
                        self._base_url,
                        json=payload,
                        headers={"X-Transmission-Session-Id": session_id},
                    )
                    rpc_header = response.headers.get("X-Transmission-Rpc-Version") or rpc_header
                if response.status_code in {401, 403}:
                    raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "Transmission 认证失败")
                if response.status_code != 200:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_CONNECTION_FAILED", "Transmission RPC 返回异常状态"
                    )
                body = response.json()
        except DownloaderAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "Transmission 连接失败或超时"
            ) from exc
        except (httpx2.HTTPError, ValueError, TypeError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 响应无法解析"
            ) from exc
        if not isinstance(body, dict):
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 响应格式无效"
            )
        return cast(dict[str, object], body), rpc_header


# --- Transmission RPC 双协议兼容 -------------------------------------------------
#
# Transmission 4.1 起把 RPC 换成 JSON-RPC 2.0：方法名与字段名统一 snake_case，
# 参数在 params 里，响应把结果放进 result 对象。4.0.x 及更早是旧信封：
# 方法名 kebab-case，参数在 arguments 里，响应是 {"result": "success", "arguments": {...}}，
# 字段名在 session-* 里是 kebab-case（download-dir），在 torrent-* 里是 camelCase
# （hashString/percentDone/torrent-added）。两者不能共用同一份 payload，这里做显式映射。

_TRANSMISSION_LEGACY_PARAM_KEYS = {
    "download_dir": "download-dir",
    "files_wanted": "files-wanted",
    "files_unwanted": "files-unwanted",
    "delete_local_data": "delete-local-data",
}
_TRANSMISSION_LEGACY_SESSION_FIELDS = {
    "download_dir": "download-dir",
    "download_dir_free_space": "download-dir-free-space",
    "rpc_version_semver": "rpc-version-semver",
}
_TRANSMISSION_LEGACY_TORRENT_FIELDS = {
    "hash_string": "hashString",
    "download_dir": "downloadDir",
    "percent_done": "percentDone",
    "recheck_progress": "recheckProgress",
    "total_size": "totalSize",
}
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake_key(key: str) -> str:
    """把旧协议的 download-dir / hashString / size-bytes 统一成 snake_case。"""

    return _CAMEL_BOUNDARY.sub("_", key.replace("-", "_")).lower()


def _snake_keys(value: object) -> object:
    if isinstance(value, dict):
        return {_snake_key(str(key)): _snake_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snake_keys(item) for item in value]
    return value


def _jsonrpc2_payload(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}


def _legacy_payload(method: str, params: dict[str, object]) -> dict[str, object]:
    arguments: dict[str, object] = {}
    for key, value in params.items():
        if key == "fields" and isinstance(value, list):
            arguments["fields"] = [_legacy_field_name(method, str(item)) for item in value]
            continue
        arguments[_TRANSMISSION_LEGACY_PARAM_KEYS.get(key, key)] = value
    return {"method": method.replace("_", "-"), "arguments": arguments, "tag": 1}


def _legacy_field_name(method: str, field: str) -> str:
    if method.startswith("torrent_"):
        return _TRANSMISSION_LEGACY_TORRENT_FIELDS.get(field, field)
    return _TRANSMISSION_LEGACY_SESSION_FIELDS.get(field, field)


def _is_jsonrpc2_envelope(body: dict[str, object]) -> bool:
    return body.get("jsonrpc") == "2.0"


def _is_legacy_envelope(body: dict[str, object]) -> bool:
    return "jsonrpc" not in body and "result" in body


def _jsonrpc2_result(body: dict[str, object]) -> dict[str, object]:
    if "error" in body:
        raise DownloaderAdapterError("DOWNLOADER_WRITE_FAILED", "Transmission RPC 返回失败结果")
    result = body.get("result")
    if not isinstance(result, dict):
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC result 格式无效"
        )
    return cast(dict[str, object], result)


def _legacy_result(body: dict[str, object]) -> dict[str, object]:
    # 旧协议把失败原因直接放在 result 字符串里（如 "method name not recognized"）。
    if body.get("result") != "success":
        raise DownloaderAdapterError("DOWNLOADER_WRITE_FAILED", "Transmission RPC 返回失败结果")
    arguments = body.get("arguments")
    if arguments is None:
        return {}
    normalized = _snake_keys(arguments)
    if not isinstance(normalized, dict):
        raise DownloaderAdapterError(
            "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC arguments 格式无效"
        )
    return cast(dict[str, object], normalized)


def _normalize_file_selection(
    wanted: tuple[int, ...],
    unwanted: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    def normalize(values: tuple[int, ...]) -> tuple[int, ...]:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("torrent 文件索引必须是非负整数")
        return tuple(sorted(set(values)))

    wanted_ids = normalize(wanted)
    unwanted_ids = normalize(unwanted)
    if set(wanted_ids) & set(unwanted_ids):
        raise ValueError("同一 torrent 文件不能同时设为 wanted 与 unwanted")
    return wanted_ids, unwanted_ids


def _remote_child(root: str, name: str) -> str:
    if not name or "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("torrent 名称不能作为安全路径段")
    if "\\" in root or (len(root) >= 2 and root[1] == ":"):
        return normalize_remote_path(str(PureWindowsPath(root) / name))
    return normalize_remote_path(str(PurePosixPath(root) / name))


def _optional_seeding_seconds(value: object) -> int | None:
    """Accept only a non-negative numeric seeding duration."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(value)
    return seconds if seconds >= 0 else None


def _optional_unix_datetime(value: object) -> datetime | None:
    """Convert a positive unix timestamp to an aware UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _qbittorrent_tracker_state(payload: list[object]) -> DownloaderTrackerState:
    """Collapse qBittorrent tracker rows into one verdict.

    qBittorrent mixes DHT/PeX/LSD pseudo-rows (url starts with "**") into the
    same payload; those are not trackers and must not affect the verdict.
    """
    entries = [
        item
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and not str(item["url"]).startswith("**")
    ]
    if not entries:
        return DownloaderTrackerState(TRACKER_STATE_UNKNOWN)
    if any(item.get("status") == _QBITTORRENT_TRACKER_WORKING for item in entries):
        return DownloaderTrackerState(TRACKER_STATE_OK)
    failed = [item for item in entries if item.get("status") == _QBITTORRENT_TRACKER_NOT_WORKING]
    if len(failed) == len(entries):
        message = next((str(item.get("msg")) for item in failed if item.get("msg")), None)
        return DownloaderTrackerState(TRACKER_STATE_ERROR, message)
    return DownloaderTrackerState(TRACKER_STATE_UNKNOWN)


def _transmission_tracker_state(raw: object) -> tuple[str, str | None, datetime | None]:
    """Collapse Transmission trackerStats into (state, message, last_announce).

    A torrent with several trackers is healthy when at least one announce
    succeeds; only an all-failed set is reported as an error.
    """
    if not isinstance(raw, list):
        return TRACKER_STATE_UNKNOWN, None, None
    stats = [item for item in raw if isinstance(item, dict)]
    if not stats:
        return TRACKER_STATE_UNKNOWN, None, None
    last_announce_at = _latest_announce_at(stats)
    if any(item.get("lastAnnounceSucceeded") is True for item in stats):
        return TRACKER_STATE_OK, None, last_announce_at
    results = [
        str(item["lastAnnounceResult"])
        for item in stats
        if isinstance(item.get("lastAnnounceResult"), str) and item["lastAnnounceResult"]
    ]
    return TRACKER_STATE_ERROR, results[0] if results else None, last_announce_at


def _latest_announce_at(stats: list[dict[str, object]]) -> datetime | None:
    stamps: list[float] = []
    for item in stats:
        value = item.get("lastAnnounceTime")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        stamp = float(value)
        if stamp > 0:
            stamps.append(stamp)
    if not stamps:
        return None
    return _optional_unix_datetime(max(stamps))
