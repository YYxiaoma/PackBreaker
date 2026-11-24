from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx2

from backend.app.domain.site_adapter import (
    SiteConnectionResult,
    SiteSeedingList,
    SiteUserProfile,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.nexusphp import (
    NexusPhpWebAdapter,
    _download_href,
    _explicit_detail_size,
    _normalize_origin,
    _parse_html,
    _parse_nexus_seeding_rows,
    _parse_nonnegative_int,
    _parse_size,
    _profile_datetime,
    _profile_float_from_parser,
    _profile_size_from_parser,
    _seeding_total_hint,
    _validate_torrent_id,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text())
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def walk(self) -> Iterator[_Node]:
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.walk()

    def find(
        self,
        *,
        tag: str | None = None,
        class_name: str | None = None,
        href_contains: str | None = None,
    ) -> list[_Node]:
        result: list[_Node] = []
        for node in self.walk():
            if tag is not None and node.tag != tag:
                continue
            if class_name is not None and class_name not in node.attrs.get("class", "").split():
                continue
            if href_contains is not None and href_contains not in node.attrs.get("href", ""):
                continue
            result.append(node)
        return result

    def attr(self, name: str) -> str:
        value = self.attrs.get(name, "")
        return value.strip() if isinstance(value, str) else ""


class _DomParser(HTMLParser):
    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: (value or "") for key, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, {key: (value or "") for key, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].children.append(data)


def _dom(html: str) -> _Node:
    parser = _DomParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _first_text(nodes: list[_Node]) -> str:
    return nodes[0].text() if nodes else ""


def _href_id(href: str) -> str | None:
    values = parse_qs(urlsplit(href).query).get("id", [])
    return values[0] if values and re.fullmatch(r"[0-9]{1,20}", values[0]) else None


def _same_origin_url(base_url: str, href: str) -> str:
    origin, _base = _normalize_origin(base_url)
    parsed = urlsplit(urljoin(origin + "/", href))
    if (parsed.scheme, parsed.netloc) != urlsplit(origin)[:2]:
        raise SiteAdapterError("SITE_DOWNLOAD_URL_INVALID", "站点下载链接跨域")
    return urljoin(origin + "/", href)


def _bencode_payload(site_id: str, torrent_id: str, content: bytes) -> TorrentPayload:
    if not content.startswith(b"d"):
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "站点响应不是 bencode 字典")
    return TorrentPayload(site_id, torrent_id, content)


def _parse_api_datetime(value: object, *, tz: timezone) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz).astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz).astimezone(UTC)
        except ValueError:
            continue
    return None


def _api_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _api_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and number < float("inf") else None


class HDAreaAdapter:
    """HDArea 专用 API 适配器：X-API-Key + REST v1 + 下载换票。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
        proxy_url: str | None = None,
        site_id: str = "hdarea",
    ) -> None:
        if not api_key.strip():
            raise ValueError("HDArea API Key 不能为空")
        origin, _origin_tuple = _normalize_origin(base_url)
        self._api_key = api_key.strip()
        self._origin = origin
        self._base_url = origin
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._proxy_url = proxy_url
        self._site_id = site_id
        self._tz = timezone(timedelta(hours=8))
        self._items: dict[str, dict[str, Any]] = {}

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=True,
            supports_douban_id=True,
            supports_category=True,
            supports_pagination=True,
            supports_detail_file_list=False,
            requires_download_token=True,
            min_request_interval_seconds=1.0,
        )

    async def test_connection(self) -> SiteConnectionResult:
        data = await self._api_get("/api/v1/user/profile")
        payload = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(payload, Mapping) or _api_number(payload.get("id")) is None:
            raise SiteAdapterError("SITE_AUTH_FAILED", "HDArea API Key 无效或会话已失效")
        return SiteConnectionResult(self._site_id)

    async def fetch_user_profile(self) -> SiteUserProfile:
        data = await self._api_get("/api/v1/user/profile")
        payload = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(payload, Mapping):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 用户资料响应格式无效")
        uid_value = _api_number(payload.get("id"))
        uploaded = _api_number(payload.get("uploaded"))
        downloaded = _api_number(payload.get("downloaded"))
        ratio = _api_float(payload.get("share_ratio"))
        if ratio is None and uploaded is not None and downloaded not in {None, 0}:
            ratio = uploaded / downloaded
        username = payload.get("username")
        level = payload.get("class_name")
        return SiteUserProfile(
            site_id=self._site_id,
            uid=str(uid_value) if uid_value is not None else None,
            username=str(username) if isinstance(username, str) and username.strip() else None,
            user_level=str(level) if isinstance(level, str) and level.strip() else None,
            uploaded_bytes=uploaded,
            downloaded_bytes=downloaded,
            ratio=ratio,
            torrents_posted=_api_number(payload.get("uploaded_torrents_count")),
            seeding_count=_api_number(payload.get("seeding_count")),
            seeding_size_bytes=_api_number(payload.get("seeding_size")),
            bonus=_api_float(payload.get("seedbonus")),
            bonus_per_hour=_api_float(payload.get("hourly_bonus")),
            join_time=_parse_api_datetime(payload.get("added"), tz=self._tz),
            last_access_at=_parse_api_datetime(payload.get("last_access"), tz=self._tz),
            message_count=_api_number(payload.get("unread_messages_count")),
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        params: dict[str, str] = {
            "page": str(query.page - 1),
            "per_page": "100",
            "sort": "added",
            "order": "desc",
        }
        for external_id in query.external_ids:
            if external_id.namespace == "imdb":
                params["imdb"] = external_id.value
            elif external_id.namespace == "douban":
                params["douban"] = external_id.value
        if "imdb" not in params and "douban" not in params:
            params["keyword"] = query.query_text
        data = await self._api_get("/api/v1/torrents", params=params)
        payload = data.get("data") if isinstance(data, Mapping) else None
        raw_items = payload.get("items") if isinstance(payload, Mapping) else None
        if payload is None or not isinstance(raw_items, list):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 搜索响应格式无效")
        items: list[CandidateMeta] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            torrent_id = str(raw.get("id") or "").strip()
            if not re.fullmatch(r"[0-9]{1,20}", torrent_id):
                continue
            title = str(raw.get("name") or "").strip()
            if not title:
                continue
            self._items[torrent_id] = dict(raw)
            size = _api_number(raw.get("size"))
            published = _parse_api_datetime(raw.get("added"), tz=self._tz)
            items.append(
                normalize_candidate_meta(
                    site_id=self._site_id,
                    torrent_id=torrent_id,
                    display_name=title,
                    total_size=size,
                    published_at=published,
                    seeders=_api_number(raw.get("seeders")),
                    leechers=_api_number(raw.get("leechers")),
                )
            )
        pagination = payload.get("pagination") if isinstance(payload, Mapping) else None
        has_more = bool(
            isinstance(pagination, Mapping)
            and (pagination.get("has_more") is True or pagination.get("hasMore") is True)
        )
        return SearchPage(self._site_id, query.page, tuple(items), has_more)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = _validate_torrent_id(torrent_id)
        raw = self._items.get(normalized_id)
        if raw is None:
            raise SiteAdapterError("SITE_NOT_FOUND", "HDArea 搜索缓存不包含该种子")
        title = str(raw.get("name") or "").strip()
        candidate = normalize_candidate_meta(
            site_id=self._site_id,
            torrent_id=normalized_id,
            display_name=title,
            total_size=_api_number(raw.get("size")),
            published_at=_parse_api_datetime(raw.get("added"), tz=self._tz),
            seeders=_api_number(raw.get("seeders")),
            leechers=_api_number(raw.get("leechers")),
        )
        return TorrentDetails(candidate)

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        normalized_id = _validate_torrent_id(torrent_id)
        data = await self._api_post(
            "/api/v1/torrents/genDlToken", json_payload={"id": int(normalized_id)}
        )
        payload = data.get("data") if isinstance(data, Mapping) else data
        download_url = ""
        if isinstance(payload, Mapping):
            candidate = payload.get("download_url")
            if isinstance(candidate, str) and candidate.strip():
                download_url = candidate.strip()
            else:
                token = payload.get("dl_token")
                if isinstance(token, str) and token.strip():
                    download_url = f"/download.php?id={normalized_id}&dl_token={token.strip()}"
        if not download_url:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 下载令牌响应缺少下载地址")
        download_url = _same_origin_url(self._base_url, download_url)
        ids = parse_qs(urlsplit(download_url).query).get("id", [])
        if ids and ids[0] != normalized_id:
            raise SiteAdapterError("SITE_DOWNLOAD_URL_INVALID", "HDArea 下载地址 ID 不匹配")
        content = await self._download_bytes(download_url)
        return _bencode_payload(self._site_id, normalized_id, content)

    async def list_seeding(self, cursor: str | None = None) -> SiteSeedingList:
        # 上游明确禁用逐种子列表；只有聚合计数可用。
        return SiteSeedingList(supported=False)

    async def _api_get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Mapping[str, object]:
        return await self._request("GET", path, params=params)

    async def _api_post(
        self, path: str, *, json_payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        return await self._request("POST", path, json_payload=json_payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
        }
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
                proxy=self._proxy_url,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    json=json_payload,
                )
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "HDArea 连接失败或超时", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SiteAdapterError(
                "SITE_HTTP_ERROR", "HDArea HTTP 请求失败", retryable=True
            ) from exc
        if response.status_code in {401, 403}:
            raise SiteAdapterError("SITE_AUTH_FAILED", "HDArea API Key 无效或权限不足")
        if response.status_code == 429:
            raise SiteAdapterError("SITE_RATE_LIMITED", "HDArea 请求达到限流", retryable=True)
        if response.status_code >= 500:
            raise SiteAdapterError("SITE_UNAVAILABLE", "HDArea 服务暂时不可用", retryable=True)
        if response.status_code != 200:
            raise SiteAdapterError("SITE_HTTP_ERROR", "HDArea 返回异常状态")
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 响应不是有效 JSON") from exc
        if not isinstance(payload, Mapping):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 响应格式无效")
        if payload.get("success") is False or payload.get("code") not in {None, 0, "0"}:
            code = str(payload.get("code", ""))
            if code.startswith("401") or code.startswith("403"):
                raise SiteAdapterError("SITE_AUTH_FAILED", "HDArea API Key 无效或权限不足")
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea API 返回业务错误")
        return payload

    async def _download_bytes(self, download_url: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async with (
                httpx2.AsyncClient(
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    transport=self._transport,
                    proxy=self._proxy_url,
                ) as client,
                client.stream("GET", download_url) as response,
            ):
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise SiteAdapterError(
                        "SITE_DOWNLOAD_URL_INVALID", "HDArea 下载地址发生未允许的重定向"
                    )
                if response.status_code == 404:
                    raise SiteAdapterError("SITE_NOT_FOUND", "HDArea 种子不存在")
                if response.status_code >= 500:
                    raise SiteAdapterError(
                        "SITE_UNAVAILABLE", "HDArea 下载服务不可用", retryable=True
                    )
                if response.status_code != 200:
                    raise SiteAdapterError("SITE_HTTP_ERROR", "HDArea 下载返回异常状态")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > 20 * 1024 * 1024:
                        raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 种子文件过大")
                    chunks.append(chunk)
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "HDArea 下载失败或超时", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SiteAdapterError(
                "SITE_HTTP_ERROR", "HDArea 下载 HTTP 错误", retryable=True
            ) from exc
        content = b"".join(chunks)
        if not content:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDArea 下载响应为空")
        return content


class CandidateNexusPhpAdapter(NexusPhpWebAdapter):
    """候选站默认关闭逐种子做种列表，避免未证据端点造成误报。"""

    async def list_seeding(self, cursor: str | None = None) -> SiteSeedingList:
        return SiteSeedingList(supported=False)


class HDCityAdapter(NexusPhpWebAdapter):
    """HDCity：/pt + iwannaseethis + /t-<id> + download?id=。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 做种列表每翻一页都要 userid；缓存本次实例的结果，避免每页重抓
        # /userdetails（一次巡查最多 40 页，会放大成 80 次额外请求）。
        self._resolved_user_id: str | None = None

    async def _resolve_user_id(self) -> str | None:
        if self._resolved_user_id is not None:
            return self._resolved_user_id
        uid = ((await self.fetch_user_profile()).uid or "").strip()
        if not uid:
            return None
        self._resolved_user_id = uid
        return uid

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=False,
            supports_douban_id=False,
            supports_category=True,
            supports_pagination=True,
            supports_detail_file_list=False,
            requires_download_token=False,
            min_request_interval_seconds=self._profile.min_request_interval_seconds,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        html = await self._get_html(
            "/pt", params={"notnewword": "1", "iwannaseethis": query.query_text}
        )
        self._reject_login_page(_parse_html(html))
        root = _dom(html)
        cards = [
            node
            for node in root.find(tag="div")
            if "mitem" in node.attrs.get("class", "").split()
            or "vertical-align:middle;padding:1em" in node.attrs.get("style", "")
        ]
        items: list[CandidateMeta] = []
        for card in cards:
            download_links = card.find(href_contains="download?id=")
            if not download_links:
                continue
            href = download_links[0].attr("href")
            torrent_id = _href_id(href)
            if torrent_id is None:
                continue
            title_nodes = card.find(href_contains="t-")
            title = _first_text(title_nodes)
            if not title:
                continue
            size_text = _first_text(card.find(class_name="mbelement")) or card.text()
            size_match = re.search(r"\[([\d.]+\s*[KMGTPE]?i?B)\]", size_text)
            if size_match:
                size_text = size_match.group(1)
            seeders = self._linked_count(card, "seeders")
            leechers = self._linked_count(card, "leechers")
            published = self._card_datetime(card)
            items.append(
                normalize_candidate_meta(
                    site_id=self._profile.site_id,
                    torrent_id=torrent_id,
                    display_name=title,
                    total_size=_parse_size(size_text),
                    published_at=published,
                    seeders=seeders,
                    leechers=leechers,
                )
            )
        return SearchPage(self._profile.site_id, query.page, tuple(items), False)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = _validate_torrent_id(torrent_id)
        parser = _parse_html(await self._get_html(f"/t-{normalized_id}"))
        self._reject_login_page(parser)
        title = parser.meta_title or parser.heading or normalized_id
        candidate = normalize_candidate_meta(
            site_id=self._profile.site_id,
            torrent_id=normalized_id,
            display_name=title,
            total_size=_explicit_detail_size(parser),
        )
        return TorrentDetails(candidate)

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        normalized_id = _validate_torrent_id(torrent_id)
        parser = _parse_html(await self._get_html(f"/t-{normalized_id}"))
        self._reject_login_page(parser)
        for href, _title, _label in parser.all_links:
            if "download?id=" not in href or _href_id(href) != normalized_id:
                continue
            content = await self._get_bytes(
                _same_origin_url(self._base_url, href), limit=self._max_torrent_bytes
            )
            return _bencode_payload(self._profile.site_id, normalized_id, content)
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "HDCity 详情页缺少下载链接")

    async def fetch_user_profile(self) -> SiteUserProfile:
        profile = await super().fetch_user_profile()
        try:
            page = _parse_html(await self._get_html("/userdetails"))
            self._reject_login_page(page)
            text = "|".join(page.text_parts)
            values: dict[str, object] = {}
            uploaded = _profile_size_from_parser(page, text, "上传量", "Uploaded")
            downloaded = _profile_size_from_parser(page, text, "下载量", "Downloaded")
            bonus = _profile_float_from_parser(page, text, "魅力值", "Karma Points")
            join_time = _profile_datetime(
                text, "加入日期", "Join date", timezone_offset_minutes=8 * 60
            )
            last_access = _profile_datetime(
                text, "最近动向", "Last Action", timezone_offset_minutes=8 * 60
            )
            from dataclasses import replace

            if uploaded is not None:
                values["uploaded_bytes"] = uploaded
            if downloaded is not None:
                values["downloaded_bytes"] = downloaded
            if bonus is not None:
                values["bonus"] = bonus
            if join_time is not None:
                values["join_time"] = join_time
            if last_access is not None:
                values["last_access_at"] = last_access
            if values:
                profile = replace(profile, **cast(dict[str, Any], values))
        except SiteAdapterError:
            pass
        return profile

    async def list_seeding(self, cursor: str | None = None) -> SiteSeedingList:
        user_id = await self._resolve_user_id()
        if user_id is None:
            # 拿不到 uid 时绝不能发空 userid：服务端可能返回他人或全站列表，
            # 做种巡查会据此得出错误的 SITE_ONLY 结论。
            return SiteSeedingList(supported=False, items=(), next_cursor=None)
        page = int(cursor or "0")
        params = {"userid": user_id, "type": "seeding"}
        if page:
            params["page"] = str(page)
        html = await self._get_html("/getusertorrentlistajax", params=params)
        parser = _parse_html(html)
        self._reject_login_page(parser)
        items = _parse_nexus_seeding_rows(parser)
        pages = [int(value) for value in re.findall(r"[?&]page=([0-9]{1,4})", html)]
        max_page = max(pages, default=0)
        return SiteSeedingList(
            supported=True,
            items=tuple(items),
            next_cursor=str(page + 1) if items and page < max_page else None,
            total_hint=_seeding_total_hint(html),
        )

    @staticmethod
    def _linked_count(card: _Node, marker: str) -> int | None:
        nodes = card.find(href_contains=marker)
        if not nodes:
            return None
        match = re.search(r"\d+", nodes[0].text())
        return int(match.group(0)) if match else None

    def _card_datetime(self, card: _Node) -> datetime | None:
        text = card.text()
        match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
        if match is None:
            return None
        try:
            return (
                datetime.strptime(match.group(0), "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone(timedelta(minutes=self._profile.timezone_offset_minutes)))
                .astimezone(UTC)
            )
        except ValueError:
            return None


class RetroFlixAdapter(CandidateNexusPhpAdapter):
    """RetroFlix：/browse + /browse/t/<id> + UTC 时区。"""

    async def search(self, query: SearchQuery) -> SearchPage:
        html = await self._get_html("/browse", params={"includingDead": "1"})
        self._reject_login_page(_parse_html(html))
        root = _dom(html)
        cards = [
            node
            for node in root.find(tag="div")
            if "col-12" in node.attrs.get("class", "").split()
            and any(node.find(href_contains="/browse/t/"))
        ]
        items: list[CandidateMeta] = []
        for card in cards:
            links = card.find(href_contains="/browse/t/")
            if not links:
                continue
            href = links[0].attr("href")
            torrent_id = _href_id(href)
            if torrent_id is None:
                continue
            title = links[0].text()
            if not title:
                continue
            children = [child for child in card.children if isinstance(child, _Node)]

            def child_value(nodes: list[_Node], index: int) -> str:
                return nodes[index].text() if len(nodes) > index else ""

            size = _parse_size(child_value(children, 2))
            seeders = _parse_nonnegative_int(child_value(children, 4))
            leechers = _parse_nonnegative_int(child_value(children, 5))
            published = None
            if len(children) > 1:
                raw = children[1].attrs.get("title") or children[1].attrs.get("data-original-title")
                published = _parse_api_datetime(raw, tz=UTC)
            items.append(
                normalize_candidate_meta(
                    site_id=self._profile.site_id,
                    torrent_id=torrent_id,
                    display_name=title,
                    total_size=size,
                    published_at=published,
                    seeders=seeders,
                    leechers=leechers,
                )
            )
        return SearchPage(self._profile.site_id, query.page, tuple(items), False)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = _validate_torrent_id(torrent_id)
        parser = _parse_html(await self._get_html(f"/browse/t/{normalized_id}"))
        self._reject_login_page(parser)
        title = parser.meta_title or parser.heading
        if not title:
            root = _dom(await self._get_html(f"/browse/t/{normalized_id}"))
            headings = root.find(tag="h3")
            title = _first_text(headings)
        candidate = normalize_candidate_meta(
            site_id=self._profile.site_id,
            torrent_id=normalized_id,
            display_name=title or normalized_id,
            total_size=_explicit_detail_size(parser),
        )
        return TorrentDetails(candidate)

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        normalized_id = _validate_torrent_id(torrent_id)
        parser = _parse_html(await self._get_html(f"/browse/t/{normalized_id}"))
        self._reject_login_page(parser)
        href = _download_href(parser.all_links, normalized_id)
        if href is None:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "RetroFlix 详情页缺少下载链接")
        content = await self._get_bytes(
            _same_origin_url(self._base_url, href), limit=self._max_torrent_bytes
        )
        return _bencode_payload(self._profile.site_id, normalized_id, content)

    async def fetch_user_profile(self) -> SiteUserProfile:
        from dataclasses import replace

        profile = await super().fetch_user_profile()
        # 上游显式禁用 seedingBonus；没有证据时不得填充做种积分。
        return replace(profile, seeding_points=None)


class CSPTAdapter(CandidateNexusPhpAdapter):
    """CSPT：卡片式种子列表，字段来自 .torrent-info-text-*。"""

    async def search(self, query: SearchQuery) -> SearchPage:
        search_area = "4" if any(e.namespace == "imdb" for e in query.external_ids) else "0"
        params = {
            "search": query.query_text,
            "incldead": "1",
            "spstate": "0",
            "search_area": search_area,
            "search_mode": "0",
            "page": str(query.page - 1),
            "notnewword": "1",
        }
        html = await self._get_html(self._profile.search_path, params=params)
        self._reject_login_page(_parse_html(html))
        root = _dom(html)
        cards = root.find(class_name="torrent-table-sub-info")
        items: list[CandidateMeta] = []
        for card in cards:
            links = card.find(href_contains="download.php?id=") or card.find(
                href_contains="details.php?id="
            )
            if not links:
                continue
            torrent_id = _href_id(links[0].attr("href"))
            if torrent_id is None:
                continue
            title_links = card.find(href_contains="details.php?id=")
            title = _first_text(title_links) or card.text()
            if not title:
                continue

            def card_field(node: _Node, name: str) -> str:
                return _first_text(node.find(class_name=f"torrent-info-text-{name}"))

            items.append(
                normalize_candidate_meta(
                    site_id=self._profile.site_id,
                    torrent_id=torrent_id,
                    display_name=title,
                    total_size=_parse_size(card_field(card, "size")),
                    published_at=self._parse_iso(card_field(card, "added")),
                    seeders=_parse_nonnegative_int(card_field(card, "seeders")),
                    leechers=_parse_nonnegative_int(card_field(card, "leechers")),
                )
            )
        return SearchPage(self._profile.site_id, query.page, tuple(items), False)

    def _parse_iso(self, value: str) -> datetime | None:
        if not value:
            return None
        return _parse_api_datetime(
            value, tz=timezone(timedelta(minutes=self._profile.timezone_offset_minutes))
        )


class HaidanAdapter(CandidateNexusPhpAdapter):
    """海胆：优先 isapi=1 JSON 搜索，保留网页用户资料链路。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._links: dict[str, tuple[str, str]] = {}

    async def search(self, query: SearchQuery) -> SearchPage:
        params: dict[str, str] = {
            "isapi": "1",
            "search": query.query_text,
            "search_mode": "0",
            "search_area": "4" if any(e.namespace == "imdb" for e in query.external_ids) else "0",
        }
        raw = await self._get_html(self._profile.search_path, params=params)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "海胆 isapi 响应不是有效 JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("code") not in {0, "0"}:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "海胆 isapi 返回业务错误")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "海胆 isapi 响应缺少 data")
        items: list[CandidateMeta] = []
        for torrent_id, raw_item in data.items():
            torrent_id = str(torrent_id)
            if not re.fullmatch(r"[0-9]{1,20}", torrent_id) or not isinstance(raw_item, Mapping):
                continue
            title = str(raw_item.get("name") or "").strip()
            if not title:
                continue
            group_id = str(raw_item.get("group_id") or "")
            if group_id:
                detail = f"/details.php?group_id={group_id}&torrent_id={torrent_id}"
            else:
                detail = f"/details.php?id={torrent_id}"
            download = str(raw_item.get("url") or "").strip()
            if download:
                self._links[torrent_id] = (detail, download)
            items.append(
                normalize_candidate_meta(
                    site_id=self._profile.site_id,
                    torrent_id=torrent_id,
                    display_name=title,
                    total_size=_api_number(raw_item.get("size")),
                    published_at=_parse_api_datetime(
                        raw_item.get("added"),
                        tz=timezone(timedelta(minutes=self._profile.timezone_offset_minutes)),
                    ),
                    seeders=_api_number(raw_item.get("seeders")),
                    leechers=_api_number(raw_item.get("leechers")),
                )
            )
        return SearchPage(self._profile.site_id, query.page, tuple(items), False)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = _validate_torrent_id(torrent_id)
        detail_url, _download = self._links.get(
            normalized_id, (f"/details.php?id={normalized_id}", "")
        )
        parser = _parse_html(await self._get_html_url(_same_origin_url(self._base_url, detail_url)))
        self._reject_login_page(parser)
        candidate = normalize_candidate_meta(
            site_id=self._profile.site_id,
            torrent_id=normalized_id,
            display_name=parser.meta_title or parser.heading or normalized_id,
            total_size=_explicit_detail_size(parser),
        )
        return TorrentDetails(candidate)

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        normalized_id = _validate_torrent_id(torrent_id)
        _detail, download = self._links.get(normalized_id, ("", ""))
        if not download:
            parser = _parse_html(
                await self._get_html(self._profile.details_path, params={"id": normalized_id})
            )
            href = _download_href(parser.all_links, normalized_id)
            if href is None:
                raise SiteAdapterError("SITE_INVALID_RESPONSE", "海胆详情页缺少下载链接")
            download = href
        content = await self._get_bytes(
            _same_origin_url(self._base_url, download), limit=self._max_torrent_bytes
        )
        return _bencode_payload(self._profile.site_id, normalized_id, content)

    async def fetch_user_profile(self) -> SiteUserProfile:
        from dataclasses import replace

        profile = await super().fetch_user_profile()
        # 海胆把「等级积分」作为做种积分语义，而非魔力值。
        try:
            page = _parse_html(
                await self._get_html("/userdetails.php", params={"id": profile.uid or ""})
            )
            self._reject_login_page(page)
            text = "|".join(page.text_parts)
            points = _profile_float_from_parser(page, text, "等级积分")
            if points is not None:
                profile = replace(profile, seeding_points=points)
        except SiteAdapterError:
            pass
        return profile
