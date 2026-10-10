from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime

import httpx2

from backend.app.domain.errors import DomainViolation
from backend.app.domain.site_adapter import (
    SiteConnectionResult,
    SiteUserProfile,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_config import SiteCredentialKind, normalize_site_credential
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.site_retry_after import retry_after_seconds
from backend.app.infrastructure.torrent_parser import parse_torrent

_BASE_URL = "https://rousi.pro"
_READ_ONLY_AUTH_PATH = "/api/points/attendance/stats"
_RESPONSE_LIMIT = 32 * 1024
_SEARCH_PATH = "/api/v1/torrents"
_SEARCH_RESPONSE_LIMIT = 2 * 1024 * 1024
_DOWNLOAD_LIMIT = 20 * 1024 * 1024
_TORRENT_ID_RE = re.compile(r"[1-9][0-9]{0,17}\Z")
_TORRENT_CONTENT_TYPES = frozenset(
    {
        "application/x-bittorrent",
        "application/octet-stream",
        "application/force-download",
        "binary/octet-stream",
        "",
    }
)


class RousiProCandidateAdapter:
    """Rousi adapter: API key for authentication, public search, Cookie for download.

    The site service only loads a persisted download Cookie for a site that
    has passed the independent production support gate. While Rousi remains
    PENDING_ADAPTER, real site creation, credential use and tasks are blocked.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
        proxy_url: str | None = None,
        download_cookie: str | None = None,
    ) -> None:
        # Direct isolated callers bypass SiteService's input checks; enforce
        # the same header safety rules before constructing any HTTP request.
        normalized_key = normalize_site_credential(SiteCredentialKind.API_KEY, api_key)
        normalized_cookie = (
            normalize_site_credential(SiteCredentialKind.COOKIE, download_cookie)
            if download_cookie is not None
            else None
        )
        if base_url != _BASE_URL:
            raise ValueError("Rousi Pro 仅允许固定 HTTPS 站点地址")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("Rousi Pro 请求超时不合法")
        self._api_key = normalized_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._proxy_url = proxy_url
        self._download_cookie = normalized_cookie

    async def capabilities(self) -> SiteSearchCapabilities:
        # Only the explicitly isolated, Cookie-enabled candidate may use the
        # validated API search metadata in place of an unverified details API.
        # Actual file identities and hashes must still come from the downloaded
        # torrent and pass the AnalysisService full verification gate.
        isolated_download = self._download_cookie is not None
        return SiteSearchCapabilities(
            supports_pagination=True,
            requires_download_token=True,
            search_results_are_complete=isolated_download,
            max_verification_candidates=1 if isolated_download else None,
            min_request_interval_seconds=2.0,
        )

    async def test_connection(self) -> SiteConnectionResult:
        data = await self._read_only_json(_READ_ONLY_AUTH_PATH, size_limit=_RESPONSE_LIMIT)
        if not isinstance(data.get("data"), dict):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro 认证响应缺少数据结构")
        return SiteConnectionResult("rousi_pro")

    async def _read_only_json(
        self,
        path: str,
        *,
        size_limit: int,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        # A strict path allowlist prevents accidentally attaching API keys to
        # unreviewed endpoints. Crucially, /api/v1/torrents is a public search
        # route with a DIFFERENT response contract when an api-token header
        # is included: authenticated requests return a broad unfiltered list.
        # Only the authentication test receives the API key. Never send Cookie
        # or API key to public search, follow redirects or print credentials.
        if path not in {_READ_ONLY_AUTH_PATH, _SEARCH_PATH}:
            raise ValueError("Rousi Pro API 只允许已审查的只读接口")
        try:
            async with (
                httpx2.AsyncClient(
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    proxy=self._proxy_url,
                ) as client,
                client.stream(
                    "GET",
                    f"{_BASE_URL}{path}",
                    headers=(
                        {"api-token": self._api_key, "Accept": "application/json"}
                        if path == _READ_ONLY_AUTH_PATH
                        else {"Accept": "application/json"}
                    ),
                    params=params,
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise SiteAdapterError("SITE_AUTH_FAILED", "Rousi Pro API Key 无效或权限不足")
                if response.status_code == 429:
                    raise SiteAdapterError(
                        "SITE_RATE_LIMITED",
                        "Rousi Pro 请求达到限流",
                        retryable=True,
                        retry_after_seconds=retry_after_seconds(response),
                    )
                if 500 <= response.status_code <= 599:
                    raise SiteAdapterError(
                        "SITE_UNAVAILABLE", "Rousi Pro 只读 API 暂时不可用", retryable=True
                    )
                if response.status_code != 200:
                    raise SiteAdapterError("SITE_HTTP_ERROR", "Rousi Pro 只读接口返回异常状态")
                if (
                    response.headers.get("content-type", "").split(";")[0].lower()
                    != "application/json"
                ):
                    raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro 认证接口未返回 JSON")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > size_limit:
                        raise SiteAdapterError(
                            "SITE_RESPONSE_TOO_LARGE", "Rousi Pro API 响应超出允许大小"
                        )
        except SiteAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            # Authentication and public keyword search are read-only GETs.
            # Transient connectivity failures can be retried only by the
            # caller's bounded site reliability / matching policy. Torrent
            # download stays outside this retry contract.
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "Rousi Pro 只读 API 连接失败", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SiteAdapterError("SITE_HTTP_ERROR", "Rousi Pro HTTP 请求异常") from exc
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro API JSON 无法解析") from exc
        if not isinstance(data, dict):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro API 响应结构未知")
        if path == _READ_ONLY_AUTH_PATH:
            if type(data.get("code")) is not int:
                raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro 认证响应格式未知")
            if data["code"] != 0:
                raise SiteAdapterError("SITE_AUTH_FAILED", "Rousi Pro API Key 未通过只读认证")
        return data

    async def fetch_user_profile(self) -> SiteUserProfile:
        raise SiteAdapterError("SITE_ADAPTER_PENDING", "Rousi Pro 用户资料功能不在版本范围")

    async def search(self, query: SearchQuery) -> SearchPage:
        # An external-ID-only query has no verified API mapping. Do not turn it
        # into an empty keyword search that could enumerate the entire tracker.
        if not query.query_text:
            raise SiteAdapterError(
                "SITE_UNSUPPORTED_QUERY", "Rousi Pro 暂不支持仅包含外部 ID 的搜索"
            )
        data = await self._read_only_json(
            _SEARCH_PATH,
            size_limit=_SEARCH_RESPONSE_LIMIT,
            params={
                "query": query.query_text,
                "limit": str(query.page_size),
                # Rousi Pro's public search endpoint uses offset/limit, not
                # page/limit. The old parameter made non-first pages repeat
                # or return a misleading NO_MATCH after an invalid response.
                "offset": str((query.page - 1) * query.page_size),
            },
        )
        # The public endpoint returns {items,limit,offset,total}. Reject the
        # old {code,data:{torrents,...}} envelope: it indicates the token-based
        # unfiltered route, not a valid keyword search.
        raw_rows = data.get("items")
        actual_size = data.get("limit")
        offset = data.get("offset")
        total = data.get("total")
        expected_offset = (query.page - 1) * query.page_size
        if (
            not isinstance(raw_rows, list)
            or len(raw_rows) > query.page_size
            or type(actual_size) is not int
            or actual_size != query.page_size
            or type(offset) is not int
            or offset != expected_offset
            or type(total) is not int
            or total < 0
            or total < offset + len(raw_rows)
            or (not raw_rows and offset < total)
        ):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro 公开搜索响应或分页格式无效")
        try:
            items = tuple(_parse_rousi_candidate(raw) for raw in raw_rows)
            if self._download_cookie is not None and any(
                item.total_size is None
                or item.total_size <= 0
                or _TORRENT_ID_RE.fullmatch(item.torrent_id) is None
                for item in items
            ):
                raise SiteAdapterError(
                    "SITE_INVALID_RESPONSE",
                    "Rousi Pro 候选缺少隔离分析所需的种子标识或文件大小",
                )
            has_more = bool(items) and (offset + len(items) < total)
            return SearchPage("rousi_pro", query.page, items, has_more, total)
        except (ValueError, TypeError) as exc:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro 搜索候选字段无效") from exc

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        raise SiteAdapterError("SITE_ADAPTER_PENDING", "Rousi Pro 种子详情尚未完成契约验收")

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        # The production factory deliberately supplies no Cookie. A working
        # API key alone never authorizes a torrent download.
        if self._download_cookie is None:
            raise SiteAdapterError("SITE_ADAPTER_PENDING", "Rousi Pro 下载 Cookie 尚未配置")
        if not _TORRENT_ID_RE.fullmatch(torrent_id):
            raise SiteAdapterError("SITE_INVALID_TORRENT_ID", "Rousi Pro 远程种子 ID 无效")
        try:
            async with (
                httpx2.AsyncClient(
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    proxy=self._proxy_url,
                ) as client,
                client.stream(
                    "GET",
                    f"{_BASE_URL}/api/v1/torrents/{torrent_id}/download",
                    # Never include api-token on the Cookie-only download path.
                    headers={
                        "Cookie": self._download_cookie,
                        "Accept": "application/x-bittorrent",
                    },
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise SiteAdapterError("SITE_AUTH_FAILED", "Rousi Pro 取种认证失败")
                if response.status_code == 429:
                    raise SiteAdapterError("SITE_RATE_LIMITED", "Rousi Pro 取种达到限流")
                if 500 <= response.status_code <= 599:
                    raise SiteAdapterError(
                        "SITE_UNAVAILABLE",
                        "Rousi Pro 取种服务暂时不可用",
                        retryable=True,
                    )
                if response.status_code != 200:
                    raise SiteAdapterError("SITE_HTTP_ERROR", "Rousi Pro 取种接口不可用")
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type not in _TORRENT_CONTENT_TYPES:
                    raise SiteAdapterError("SITE_INVALID_RESPONSE", "Rousi Pro 取种未返回 Torrent")
                declared_length = response.headers.get("content-length", "")
                if declared_length.isdecimal() and int(declared_length) > _DOWNLOAD_LIMIT:
                    raise SiteAdapterError(
                        "SITE_RESPONSE_TOO_LARGE", "Rousi Pro Torrent 超出大小限制"
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _DOWNLOAD_LIMIT:
                        raise SiteAdapterError(
                            "SITE_RESPONSE_TOO_LARGE", "Rousi Pro Torrent 超出大小限制"
                        )
        except SiteAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError):
            raise SiteAdapterError("SITE_UNAVAILABLE", "Rousi Pro 取种连接失败") from None
        except httpx2.HTTPError:
            raise SiteAdapterError("SITE_HTTP_ERROR", "Rousi Pro 取种 HTTP 异常") from None
        try:
            parse_torrent(bytes(raw))
        except DomainViolation:
            raise SiteAdapterError(
                "SITE_INVALID_RESPONSE", "Rousi Pro Torrent 元信息无效"
            ) from None
        return TorrentPayload("rousi_pro", torrent_id, bytes(raw))


def _parse_rousi_candidate(raw: object) -> CandidateMeta:
    if not isinstance(raw, dict):
        raise ValueError("Rousi candidate row must be a mapping")
    torrent_id = raw.get("id")
    title = raw.get("name")
    if type(torrent_id) is not int or torrent_id < 1 or not isinstance(title, str):
        raise ValueError("Rousi candidate lacks a valid identity or title")
    title = title.strip()
    if not title or len(title) > 1024:
        raise ValueError("Rousi candidate title is missing or too long")
    size = _nonnegative_int(raw.get("size_bytes"))
    seeders = _nonnegative_int(raw.get("seeders"))
    leechers = _nonnegative_int(raw.get("leechers"))
    category = raw.get("category")
    if isinstance(category, dict):
        category = category.get("name")
    if not isinstance(category, str) or not category.strip() or len(category) > 128:
        category = None
    date = raw.get("uploaded_at")
    published = None
    if isinstance(date, str):
        try:
            parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                published = parsed
        except ValueError:
            pass
    return normalize_candidate_meta(
        site_id="rousi_pro",
        torrent_id=str(torrent_id),
        display_name=title,
        total_size=size,
        seeders=seeders,
        leechers=leechers,
        category=category,
        published_at=published,
    )


def _nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Rousi candidate numeric value is invalid")
    return value
