from __future__ import annotations

from base64 import b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx2

from backend.app.domain.downloader import (
    ConnectionTestResult,
    DownloaderCapabilities,
    DownloaderCredential,
    normalize_remote_path,
)
from backend.app.domain.verification import DownloaderKind, VerificationLevel

_MAX_TORRENT_UPLOAD_BYTES = 20 * 1024 * 1024
_QBITTORRENT_JSON_ADD_API_MIN = (2, 14, 0)
_QBITTORRENT_SKIP_CHECKING_REMOVED = (2, 16, 0)
_STOPPED_STATES = frozenset({"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"})
_CHECKING_STATES = frozenset({"checkingDL", "checkingUP"})
_STOPPED_UPLOAD_STATES = frozenset({"stoppedUP", "pausedUP"})
_STOPPED_DOWNLOAD_STATES = frozenset({"stoppedDL", "pausedDL"})
_SEEDING_STATES = frozenset({"uploading", "stalledUP", "queuedUP", "forcedUP"})
_TRANSMISSION_STOPPED = 0
_TRANSMISSION_CHECKING_STATES = frozenset({1, 2})
_TRANSMISSION_SEEDING_STATES = frozenset({5, 6})


class DownloaderAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DownloaderProbeAdapter(Protocol):
    async def test_connection(self) -> ConnectionTestResult: ...


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
        if self.skip_checking and self.verification_level is not VerificationLevel.FULL_VERIFIED:
            raise ValueError("只有 FULL_VERIFIED 才允许 qBittorrent skip_checking")
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
            )
        )

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        if (
            request.skip_checking
            and request.verification_level is not VerificationLevel.FULL_VERIFIED
        ):
            raise DownloaderAdapterError(
                "DOWNLOADER_SKIP_CHECKING_BLOCKED",
                "只有 FULL_VERIFIED 才允许 qBittorrent skip_checking",
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

    async def stop_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("stop", torrent_hash)

    async def start_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("start", torrent_hash)

    async def recheck_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("recheck", torrent_hash)

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
                    if login.status_code in {401, 403} or not login.text.strip().startswith("Ok."):
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
            )
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

    async def stop_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("torrent_stop", torrent_hash)

    async def start_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("torrent_start", torrent_hash)

    async def verify_torrent(self, torrent_hash: str) -> None:
        await self._torrent_action("torrent_verify", torrent_hash)

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
        auth: httpx2.Auth | None = None
        if self._credential is not None and self._credential.username:
            auth = httpx2.BasicAuth(
                self._credential.username,
                self._credential.password or "",
            )
        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
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
                if not isinstance(body, dict):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 响应格式无效"
                    )
                if body.get("jsonrpc") != "2.0" or body.get("id") != 1:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "Transmission JSON-RPC 响应标识无效"
                    )
                if "error" in body:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_WRITE_FAILED", "Transmission RPC 返回失败结果"
                    )
                result = body.get("result")
                if not isinstance(result, dict):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC result 格式无效"
                    )
                return cast(dict[str, object], result), rpc_header
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
