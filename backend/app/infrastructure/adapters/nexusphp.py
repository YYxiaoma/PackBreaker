from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import SplitResult, parse_qs, urlencode, urljoin, urlsplit

import httpx2

from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SearchSortHint,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError

_DEFAULT_HTML_LIMIT_BYTES = 5 * 1024 * 1024
_DEFAULT_TORRENT_LIMIT_BYTES = 20 * 1024 * 1024
_IMDB_RE = re.compile(r"/title/(tt\d{5,10})", re.IGNORECASE)
_DOUBAN_RE = re.compile(r"/subject/(\d{3,12})")
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B)\s*$", re.IGNORECASE)
_TORRENT_ID_RE = re.compile(r"^\d{1,64}$")


@dataclass(frozen=True, slots=True)
class NexusPhpProfile:
    site_id: str
    default_base_url: str
    index_path: str = "/index.php"
    search_path: str = "/torrents.php"
    details_path: str = "/details.php"
    authenticated_href: str = "usercp.php"
    timezone_offset_minutes: int = 0
    date_cell_from_end: int = 6
    size_cell_from_end: int = 5
    seeders_cell_from_end: int = 4
    leechers_cell_from_end: int = 3
    min_request_interval_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.site_id.strip():
            raise ValueError("NexusPHP profile 必须包含 site_id")
        parsed = urlsplit(self.default_base_url.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("NexusPHP profile 默认地址必须是 HTTPS origin")
        for path in (self.index_path, self.search_path, self.details_path):
            if not path.startswith("/") or "?" in path or "#" in path:
                raise ValueError("NexusPHP profile 路径必须是绝对站内路径且不含 query/fragment")
        if not -14 * 60 <= self.timezone_offset_minutes <= 14 * 60:
            raise ValueError("NexusPHP profile 时区偏移超出范围")
        for offset in (
            self.date_cell_from_end,
            self.size_cell_from_end,
            self.seeders_cell_from_end,
            self.leechers_cell_from_end,
        ):
            if offset < 1:
                raise ValueError("NexusPHP profile 列偏移必须为正数")
        if self.min_request_interval_seconds < 0:
            raise ValueError("NexusPHP profile 请求间隔不能为负数")


HDTIME_PROFILE = NexusPhpProfile(
    site_id="hdtime",
    default_base_url="https://hdtime.org",
    timezone_offset_minutes=8 * 60,
    date_cell_from_end=6,
    size_cell_from_end=5,
    seeders_cell_from_end=4,
    leechers_cell_from_end=3,
    min_request_interval_seconds=0.0,
)


@dataclass(slots=True)
class _Row:
    cells: list[str] = field(default_factory=list)
    cell_titles: list[list[str]] = field(default_factory=list)
    links: list[tuple[str, str | None, str]] = field(default_factory=list)
    active_cell: int | None = None


class _NexusHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._rows: list[_Row] = []
        self._row_stack: list[_Row] = []
        self._link_stack: list[tuple[str, str | None, list[str]]] = []
        self._heading_depth = 0
        self._heading_text: list[str] = []
        self._title_depth = 0
        self._title_text: list[str] = []
        self.all_links: list[tuple[str, str | None, str]] = []

    @property
    def rows(self) -> tuple[_Row, ...]:
        return tuple(self._rows)

    @property
    def heading(self) -> str | None:
        value = _clean_text(" ".join(self._heading_text))
        return value or None

    @property
    def page_title(self) -> str | None:
        value = _clean_text(" ".join(self._title_text))
        return value or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._row_stack.append(_Row())
        elif tag == "td" and self._row_stack:
            row = self._row_stack[-1]
            row.cells.append("")
            row.cell_titles.append([])
            row.active_cell = len(row.cells) - 1
        elif tag == "span" and self._row_stack:
            title = attributes.get("title")
            row = self._row_stack[-1]
            if title and row.active_cell is not None:
                row.cell_titles[row.active_cell].append(title)
        elif tag == "a":
            href = attributes.get("href") or ""
            self._link_stack.append((href, attributes.get("title"), []))
        elif tag == "h1":
            self._heading_depth += 1
        elif tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row_stack:
            self._rows.append(self._row_stack.pop())
        elif tag == "td" and self._row_stack:
            self._row_stack[-1].active_cell = None
        elif tag == "a" and self._link_stack:
            href, title, parts = self._link_stack.pop()
            link = (href, title, _clean_text(" ".join(parts)))
            self.all_links.append(link)
            for row in self._row_stack:
                row.links.append(link)
        elif tag == "h1" and self._heading_depth:
            self._heading_depth -= 1
        elif tag == "title" and self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data: str) -> None:
        if not data:
            return
        for row in self._row_stack:
            if row.active_cell is not None:
                row.cells[row.active_cell] += data
        if self._link_stack:
            self._link_stack[-1][2].append(data)
        if self._heading_depth:
            self._heading_text.append(data)
        if self._title_depth:
            self._title_text.append(data)


class NexusPhpWebAdapter:
    """Cookie-authenticated NexusPHP Web read-only adapter driven by a site profile."""

    def __init__(
        self,
        cookie: str,
        *,
        profile: NexusPhpProfile,
        base_url: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        max_html_bytes: int = _DEFAULT_HTML_LIMIT_BYTES,
        max_torrent_bytes: int = _DEFAULT_TORRENT_LIMIT_BYTES,
    ) -> None:
        if not cookie.strip() or len(cookie) > 8192 or "\r" in cookie or "\n" in cookie:
            raise ValueError("NexusPHP Cookie 无效")
        if timeout_seconds <= 0 or max_html_bytes <= 0 or max_torrent_bytes <= 0:
            raise ValueError("NexusPHP adapter 资源限制必须为正数")
        self._cookie = cookie.strip()
        self._profile = profile
        self._base_url, self._origin = _normalize_origin(base_url or profile.default_base_url)
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_html_bytes = max_html_bytes
        self._max_torrent_bytes = max_torrent_bytes

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=True,
            supports_douban_id=True,
            supports_category=True,
            supports_pagination=True,
            supports_detail_file_list=False,
            requires_download_token=False,
            min_request_interval_seconds=self._profile.min_request_interval_seconds,
        )

    async def test_connection(self) -> SiteConnectionResult:
        parser = _parse_html(await self._get_html(self._profile.index_path))
        if not any(
            _href_path(href).endswith(self._profile.authenticated_href)
            for href, _, _ in parser.all_links
        ):
            raise SiteAdapterError("SITE_AUTH_FAILED", "NexusPHP Cookie 无效或会话已失效")
        return SiteConnectionResult(self._profile.site_id)

    async def search(self, query: SearchQuery) -> SearchPage:
        params = {
            "search": _search_value(query),
            "incldead": "1",
            "spstate": "0",
            "search_area": "0",
            "search_mode": "0",
            "sort": _sort_value(query.sort),
            "type": "desc",
            "notnewword": "1",
            "page": str(query.page - 1),
        }
        parser = _parse_html(await self._get_html(self._profile.search_path, params=params))
        self._reject_login_page(parser)
        return SearchPage(
            self._profile.site_id,
            query.page,
            _parse_search_rows(parser, self._profile),
            _has_next_page(parser, query.page),
        )

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = _validate_torrent_id(torrent_id)
        parser = _parse_html(
            await self._get_html(self._profile.details_path, params={"id": normalized_id})
        )
        self._reject_login_page(parser)
        for candidate in _parse_search_rows(parser, self._profile):
            if candidate.torrent_id == normalized_id:
                return TorrentDetails(candidate)
        display_name = parser.heading or _page_title_candidate(parser.page_title)
        if display_name is None:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP 详情页缺少可识别标题")
        return TorrentDetails(
            normalize_candidate_meta(
                site_id=self._profile.site_id,
                torrent_id=normalized_id,
                display_name=display_name,
                external_ids=_external_ids(parser.all_links),
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        normalized_id = _validate_torrent_id(torrent_id)
        parser = _parse_html(
            await self._get_html(self._profile.details_path, params={"id": normalized_id})
        )
        self._reject_login_page(parser)
        download_href = _download_href(parser.all_links, normalized_id)
        if download_href is None:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP 详情页缺少 torrent 下载链接")
        content = await self._get_bytes(
            self._same_origin_url(download_href), limit=self._max_torrent_bytes
        )
        if not content.startswith(b"d"):
            raise SiteAdapterError(
                "SITE_INVALID_RESPONSE", "NexusPHP torrent 响应不是 bencode 字典"
            )
        return TorrentPayload(self._profile.site_id, normalized_id, content)

    async def _get_html(self, path: str, *, params: dict[str, str] | None = None) -> str:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        raw = await self._get_bytes(url, limit=self._max_html_bytes, accept="text/html")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP HTML 不是有效 UTF-8") from exc

    async def _get_bytes(
        self, url: str, *, limit: int, accept: str = "application/x-bittorrent"
    ) -> bytes:
        self._assert_same_origin(url)
        chunks: list[bytes] = []
        total = 0
        try:
            async with (
                httpx2.AsyncClient(
                    headers={"Accept": accept, "Cookie": self._cookie},
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
                client.stream("GET", url) as response,
            ):
                self._raise_for_status(response)
                length = _content_length(response)
                if length is not None and length > limit:
                    raise SiteAdapterError("SITE_RESPONSE_TOO_LARGE", "NexusPHP 响应超过大小上限")
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise SiteAdapterError(
                            "SITE_RESPONSE_TOO_LARGE", "NexusPHP 响应超过大小上限"
                        )
                    chunks.append(chunk)
        except SiteAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "NexusPHP 连接失败或超时", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise SiteAdapterError(
                "SITE_HTTP_ERROR", "NexusPHP HTTP 请求失败", retryable=True
            ) from exc
        content = b"".join(chunks)
        if not content:
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP 响应为空")
        return content

    def _raise_for_status(self, response: httpx2.Response) -> None:
        if response.status_code in {301, 302, 303, 307, 308}:
            if "login.php" in response.headers.get("location", ""):
                raise SiteAdapterError("SITE_AUTH_FAILED", "NexusPHP Cookie 无效或会话已失效")
            raise SiteAdapterError("SITE_HTTP_ERROR", "NexusPHP 返回了未允许的重定向")
        if response.status_code in {401, 403}:
            raise SiteAdapterError("SITE_AUTH_FAILED", "NexusPHP Cookie 无效或权限不足")
        if response.status_code == 429:
            raise SiteAdapterError("SITE_RATE_LIMITED", "NexusPHP 请求达到限流", retryable=True)
        if response.status_code >= 500:
            raise SiteAdapterError("SITE_UNAVAILABLE", "NexusPHP 站点暂时不可用", retryable=True)
        if response.status_code != 200:
            raise SiteAdapterError("SITE_HTTP_ERROR", "NexusPHP 返回异常状态")

    def _reject_login_page(self, parser: _NexusHtmlParser) -> None:
        has_login = any("login.php" in _href_path(href) for href, _, _ in parser.all_links)
        authenticated = any(
            _href_path(href).endswith(self._profile.authenticated_href)
            for href, _, _ in parser.all_links
        )
        if has_login and not authenticated:
            raise SiteAdapterError("SITE_AUTH_FAILED", "NexusPHP Cookie 无效或会话已失效")

    def _same_origin_url(self, href: str) -> str:
        url = urljoin(f"{self._base_url}/", href)
        self._assert_same_origin(url)
        return url

    def _assert_same_origin(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            origin = _origin_tuple(parsed)
        except ValueError as exc:
            raise SiteAdapterError("SITE_DOWNLOAD_URL_INVALID", "NexusPHP URL 格式无效") from exc
        if (
            origin != self._origin
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise SiteAdapterError(
                "SITE_DOWNLOAD_URL_INVALID", "NexusPHP URL 不属于配置站点 origin"
            )


class HDTimeAdapter(NexusPhpWebAdapter):
    def __init__(
        self,
        cookie: str,
        *,
        base_url: str = HDTIME_PROFILE.default_base_url,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        max_html_bytes: int = _DEFAULT_HTML_LIMIT_BYTES,
        max_torrent_bytes: int = _DEFAULT_TORRENT_LIMIT_BYTES,
    ) -> None:
        super().__init__(
            cookie,
            profile=HDTIME_PROFILE,
            base_url=base_url,
            transport=transport,
            timeout_seconds=timeout_seconds,
            max_html_bytes=max_html_bytes,
            max_torrent_bytes=max_torrent_bytes,
        )


def _normalize_origin(value: str) -> tuple[str, tuple[str, str, int | None]]:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("NexusPHP base URL 必须是无凭证、无路径的 HTTPS origin")
    return f"https://{parsed.netloc}", _origin_tuple(parsed)


def _origin_tuple(parsed: SplitResult) -> tuple[str, str, int | None]:
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    port = parsed.port
    return scheme, hostname, None if scheme == "https" and port == 443 else port


def _parse_html(value: str) -> _NexusHtmlParser:
    parser = _NexusHtmlParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP HTML 无法解析") from exc
    return parser


def _parse_search_rows(
    parser: _NexusHtmlParser, profile: NexusPhpProfile
) -> tuple[CandidateMeta, ...]:
    best_by_id: dict[str, tuple[int, CandidateMeta]] = {}
    required_offset = max(
        profile.date_cell_from_end,
        profile.size_cell_from_end,
        profile.seeders_cell_from_end,
        profile.leechers_cell_from_end,
    )
    for row in parser.rows:
        details = _details_link(row.links)
        if details is None or len(row.cells) < required_offset:
            continue
        torrent_id, title = details
        candidate = normalize_candidate_meta(
            site_id=profile.site_id,
            torrent_id=torrent_id,
            display_name=title,
            total_size=_parse_size(_cell(row, profile.size_cell_from_end)),
            published_at=_parse_date(row, profile),
            category=_category(row.links),
            seeders=_parse_nonnegative_int(_cell(row, profile.seeders_cell_from_end)),
            leechers=_parse_nonnegative_int(_cell(row, profile.leechers_cell_from_end)),
            external_ids=_external_ids(row.links),
        )
        current = best_by_id.get(torrent_id)
        if current is None or len(row.cells) > current[0]:
            best_by_id[torrent_id] = (len(row.cells), candidate)
    return tuple(item[1] for item in best_by_id.values())


def _details_link(links: list[tuple[str, str | None, str]]) -> tuple[str, str] | None:
    for href, title, text in links:
        parsed = urlsplit(href)
        if not parsed.path.endswith("details.php"):
            continue
        values = parse_qs(parsed.query).get("id")
        if not values or _TORRENT_ID_RE.fullmatch(values[0]) is None:
            continue
        display = _clean_text(title or text)
        if display:
            return values[0], display
    return None


def _download_href(links: list[tuple[str, str | None, str]], torrent_id: str) -> str | None:
    for href, _, _ in links:
        parsed = urlsplit(href)
        if not parsed.path.endswith("download.php"):
            continue
        query = parse_qs(parsed.query)
        ids = query.get("id")
        if ids and ids[0] == torrent_id:
            return href
        if "downhash" in query:
            return href
    return None


def _search_value(query: SearchQuery) -> str:
    for namespace in ("imdb", "douban"):
        for external_id in query.external_ids:
            if external_id.namespace == namespace:
                return external_id.value
    return query.query_text


def _sort_value(sort: SearchSortHint) -> str:
    if sort is SearchSortHint.SEEDERS:
        return "7"
    if sort is SearchSortHint.NEWEST:
        return "4"
    return "1"


def _has_next_page(parser: _NexusHtmlParser, current_page: int) -> bool:
    for href, _, _ in parser.all_links:
        pages = parse_qs(urlsplit(href).query).get("page")
        if pages and pages[0].isdigit() and int(pages[0]) == current_page:
            return True
    return False


def _parse_date(row: _Row, profile: NexusPhpProfile) -> datetime | None:
    index = len(row.cells) - profile.date_cell_from_end
    candidates = [*row.cell_titles[index], row.cells[index]]
    tz = timezone(timedelta(minutes=profile.timezone_offset_minutes))
    for raw in candidates:
        value = _clean_text(raw)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=tz)
            except ValueError:
                pass
    return None


def _cell(row: _Row, from_end: int) -> str:
    return row.cells[len(row.cells) - from_end]


def _parse_size(value: str) -> int | None:
    match = _SIZE_RE.fullmatch(_clean_text(value))
    if match is None:
        return None
    powers = {
        "b": 0,
        "kb": 1,
        "kib": 1,
        "mb": 2,
        "mib": 2,
        "gb": 3,
        "gib": 3,
        "tb": 4,
        "tib": 4,
        "pb": 5,
        "pib": 5,
        "eb": 6,
        "eib": 6,
    }
    return int(float(match.group(1)) * (1024 ** powers[match.group(2).casefold()]))


def _parse_nonnegative_int(value: str) -> int | None:
    normalized = _clean_text(value).replace(",", "")
    return int(normalized) if normalized.isdecimal() else None


def _category(links: list[tuple[str, str | None, str]]) -> str | None:
    for href, _, _ in links:
        values = parse_qs(urlsplit(href).query).get("cat")
        if values and values[0].isdigit():
            return values[0]
    return None


def _external_ids(links: list[tuple[str, str | None, str]]) -> tuple[ExternalMediaId, ...]:
    result: dict[tuple[str, str], ExternalMediaId] = {}
    for href, _, _ in links:
        if (match := _IMDB_RE.search(href)) is not None:
            item = ExternalMediaId("imdb", match.group(1).casefold())
            result[(item.namespace, item.value)] = item
        elif (match := _DOUBAN_RE.search(href)) is not None:
            item = ExternalMediaId("douban", match.group(1))
            result[(item.namespace, item.value)] = item
    return tuple(result[key] for key in sorted(result))


def _page_title_candidate(value: str | None) -> str | None:
    if value is None:
        return None
    parts = [_clean_text(part) for part in value.split("::")]
    meaningful = [part for part in parts if part and "nexusphp" not in part.casefold()]
    return meaningful[-1] if len(meaningful) >= 2 else None


def _validate_torrent_id(value: str) -> str:
    normalized = value.strip()
    if _TORRENT_ID_RE.fullmatch(normalized) is None:
        raise ValueError("NexusPHP torrent ID 必须是有限长度十进制 ID")
    return normalized


def _href_path(value: str) -> str:
    return urlsplit(value).path


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _content_length(response: httpx2.Response) -> int | None:
    value = response.headers.get("content-length")
    return int(value) if value is not None and value.isdecimal() else None
