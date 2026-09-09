from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2

from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_adapter import (
    SiteAdapter,
    SiteConnectionResult,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_config import SiteKind
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchMediaType,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError as SiteAdapterError

_MTEAM_SITE_ID = "mteam"
_MTEAM_DEFAULT_BASE_URL = "https://api.m-team.cc"
_MTEAM_TORRENT_LIMIT_BYTES = 20 * 1024 * 1024
_MTEAM_MIN_REQUEST_INTERVAL_SECONDS = 90.0
_IMDB_ID_RE = re.compile(r"tt\d{5,10}", re.IGNORECASE)
_DOUBAN_ID_RE = re.compile(r"\d{3,12}")


class SiteAdapterFactory:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def create(self, *, kind: SiteKind, base_url: str, api_key: str) -> SiteAdapter:
        if kind is SiteKind.MTEAM:
            return MTeamAdapter(api_key, base_url=base_url, transport=self._transport)
        raise ValueError("暂不支持该站点类型")


class MTeamAdapter:
    """M-Team 官方 API 的只读适配器边界；不持久化 API Key 或下载 URL。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _MTEAM_DEFAULT_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        max_torrent_bytes: int = _MTEAM_TORRENT_LIMIT_BYTES,
    ) -> None:
        if not api_key.strip():
            raise ValueError("M-Team API Key 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("M-Team timeout 必须为正数")
        if max_torrent_bytes <= 0:
            raise ValueError("torrent payload 上限必须为正数")
        self._api_key = api_key
        self._base_url, self._base_host, self._download_host_suffix = _normalize_api_base(base_url)
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_torrent_bytes = max_torrent_bytes

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=True,
            supports_douban_id=True,
            supports_category=True,
            supports_pagination=True,
            supports_detail_file_list=False,
            requires_download_token=True,
            min_request_interval_seconds=_MTEAM_MIN_REQUEST_INTERVAL_SECONDS,
        )

    async def test_connection(self) -> SiteConnectionResult:
        data = await self._post_api("/api/member/profile")
        if not isinstance(data, Mapping):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team profile 响应格式无效")
        return SiteConnectionResult(_MTEAM_SITE_ID)

    async def search(self, query: SearchQuery) -> SearchPage:
        payload: dict[str, object] = {
            "pageNumber": query.page,
            "pageSize": query.page_size,
            "mode": _mteam_mode(query.media_type),
        }
        if query.query_text:
            payload["keyword"] = query.query_text
        for external_id in query.external_ids:
            if external_id.namespace == "imdb":
                payload["imdb"] = f"https://www.imdb.com/title/{external_id.value}/"
            elif external_id.namespace == "douban":
                payload["douban"] = f"https://movie.douban.com/subject/{external_id.value}/"

        data = await self._post_api("/api/torrent/search", json=payload)
        if not isinstance(data, Mapping):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team 搜索响应格式无效")
        raw_items = data.get("data")
        if not isinstance(raw_items, list):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team 搜索结果列表格式无效")
        items = tuple(_parse_mteam_candidate(item) for item in raw_items)
        total_hint = _optional_nonnegative_int(data.get("total"))
        has_more = total_hint is not None and query.page * query.page_size < total_hint
        return SearchPage(_MTEAM_SITE_ID, query.page, items, has_more, total_hint)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = _validate_torrent_id(torrent_id)
        data = await self._post_api("/api/torrent/detail", form={"id": normalized_id})
        return TorrentDetails(_parse_mteam_candidate(data))

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        normalized_id = _validate_torrent_id(torrent_id)
        token_data = await self._post_api("/api/torrent/genDlToken", form={"id": normalized_id})
        if not isinstance(token_data, str):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team 下载令牌响应格式无效")
        download_url = self._validate_download_url(token_data)
        content = await self._download_bounded(download_url)
        return TorrentPayload(_MTEAM_SITE_ID, normalized_id, content)

    async def _post_api(
        self,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> object:
        headers = {"Accept": "application/json", "x-api-key": self._api_key}
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(f"{self._base_url}{path}", json=json, data=form)
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "M-Team 连接失败或超时", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SiteAdapterError(
                "SITE_HTTP_ERROR", "M-Team HTTP 请求失败", retryable=True
            ) from exc
        self._raise_for_http_status(response)
        try:
            envelope = response.json()
        except (ValueError, TypeError) as exc:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team API 响应不是有效 JSON") from exc
        if not isinstance(envelope, Mapping):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team API envelope 格式无效")
        code = str(envelope.get("code", ""))
        if code not in {"0", "200"}:
            if code in {"401", "403"}:
                raise SiteAdapterError("SITE_AUTH_FAILED", "M-Team API Key 无效或权限不足")
            raise SiteAdapterError("SITE_API_REJECTED", "M-Team API 拒绝请求")
        if "data" not in envelope:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team API 响应缺少 data")
        return envelope["data"]

    def _raise_for_http_status(self, response: httpx2.Response) -> None:
        if response.status_code in {401, 403}:
            raise SiteAdapterError("SITE_AUTH_FAILED", "M-Team API Key 无效或权限不足")
        if response.status_code == 429:
            raise SiteAdapterError(
                "SITE_RATE_LIMITED",
                "M-Team API 请求达到限流",
                retryable=True,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code >= 500:
            raise SiteAdapterError("SITE_UNAVAILABLE", "M-Team API 暂时不可用", retryable=True)
        if response.status_code != 200:
            raise SiteAdapterError("SITE_HTTP_ERROR", "M-Team API 返回异常状态")

    def _validate_download_url(self, value: str) -> str:
        try:
            parsed = urlsplit(value.strip())
            host = parsed.hostname
        except ValueError as exc:
            raise SiteAdapterError("SITE_DOWNLOAD_URL_INVALID", "M-Team 下载 URL 无效") from exc
        if (
            parsed.scheme != "https"
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not _host_matches_suffix(host, self._download_host_suffix)
        ):
            raise SiteAdapterError("SITE_DOWNLOAD_URL_INVALID", "M-Team 下载 URL 不在允许域")
        return value.strip()

    async def _download_bounded(self, download_url: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async with (
                httpx2.AsyncClient(
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
                client.stream("GET", download_url) as response,
            ):
                if response.status_code >= 500:
                    raise SiteAdapterError(
                        "SITE_UNAVAILABLE", "M-Team torrent 下载暂时不可用", retryable=True
                    )
                if response.status_code != 200:
                    raise SiteAdapterError("SITE_TORRENT_FETCH_FAILED", "M-Team torrent 下载失败")
                length = _optional_nonnegative_int(response.headers.get("content-length"))
                if length is not None and length > self._max_torrent_bytes:
                    raise SiteAdapterError("SITE_TORRENT_TOO_LARGE", "M-Team torrent 超过大小上限")
                async for chunk in _response_chunks(response):
                    total += len(chunk)
                    if total > self._max_torrent_bytes:
                        raise SiteAdapterError(
                            "SITE_TORRENT_TOO_LARGE", "M-Team torrent 超过大小上限"
                        )
                    chunks.append(chunk)
        except SiteAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "M-Team torrent 下载失败或超时", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SiteAdapterError(
                "SITE_TORRENT_FETCH_FAILED", "M-Team torrent HTTP 请求失败"
            ) from exc
        content = b"".join(chunks)
        if not content:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team torrent payload 为空")
        return content


async def _response_chunks(response: httpx2.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes():
        if chunk:
            yield chunk


def _normalize_api_base(value: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ValueError("M-Team API base URL 无效") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("M-Team API base URL 必须是无凭证、无路径的 HTTPS origin")
    host = parsed.hostname.casefold()
    parts = host.split(".")
    suffix = ".".join(parts[1:]) if len(parts) >= 3 and parts[0] == "api" else host
    return f"https://{parsed.netloc}", host, suffix


def _host_matches_suffix(host: str, suffix: str) -> bool:
    normalized = host.casefold().rstrip(".")
    return normalized == suffix or normalized.endswith(f".{suffix}")


def _mteam_mode(media_type: SearchMediaType) -> str:
    if media_type is SearchMediaType.MOVIE:
        return "movie"
    if media_type is SearchMediaType.TV:
        return "tvshow"
    return "normal"


def _validate_torrent_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 64 or not normalized.isdecimal():
        raise ValueError("M-Team torrent ID 必须是有限长度十进制 ID")
    return normalized


def _parse_mteam_candidate(value: object) -> CandidateMeta:
    if not isinstance(value, Mapping):
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "M-Team torrent 条目格式无效")
    torrent_id = _required_text(value.get("id"), "torrent ID", max_length=64)
    display_name = _required_text(value.get("name"), "torrent 标题", max_length=1024)
    status = value.get("status")
    status_mapping = status if isinstance(status, Mapping) else {}
    return normalize_candidate_meta(
        site_id=_MTEAM_SITE_ID,
        torrent_id=torrent_id,
        display_name=display_name,
        total_size=_optional_nonnegative_int(value.get("size")),
        published_at=_optional_aware_datetime(value.get("createdDate")),
        category=_optional_text(value.get("category"), max_length=128),
        seeders=_optional_nonnegative_int(status_mapping.get("seeders")),
        leechers=_optional_nonnegative_int(status_mapping.get("leechers")),
        external_ids=_extract_external_ids(value),
    )


def _extract_external_ids(value: Mapping[object, object]) -> tuple[ExternalMediaId, ...]:
    result: list[ExternalMediaId] = []
    imdb = _optional_text(value.get("imdb"), max_length=512)
    if imdb is not None and (match := _IMDB_ID_RE.search(imdb)) is not None:
        result.append(ExternalMediaId("imdb", match.group(0).casefold()))
    douban = _optional_text(value.get("douban"), max_length=512)
    if douban is not None and (match := _DOUBAN_ID_RE.search(douban)) is not None:
        result.append(ExternalMediaId("douban", match.group(0)))
    return tuple(result)


def _required_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise SiteAdapterError("SITE_INVALID_RESPONSE", f"M-Team {label}格式无效")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise SiteAdapterError("SITE_INVALID_RESPONSE", f"M-Team {label}格式无效")
    return normalized


def _optional_text(value: object, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= max_length else None


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _retry_after_seconds(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
