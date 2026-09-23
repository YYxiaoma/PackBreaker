from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import SplitResult, parse_qs, urlencode, urljoin, urlsplit

import httpx2

from backend.app.domain.errors import DomainViolation
from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_adapter import (
    SiteConnectionResult,
    SiteUserProfile,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SearchSortHint,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.torrent_parser import parse_torrent

_DEFAULT_HTML_LIMIT_BYTES = 5 * 1024 * 1024
_DEFAULT_TORRENT_LIMIT_BYTES = 20 * 1024 * 1024
_IMDB_RE = re.compile(r"/title/(tt\d{5,10})", re.IGNORECASE)
_DOUBAN_RE = re.compile(r"/subject/(\d{3,12})")
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B)\s*$", re.IGNORECASE)
_TORRENT_ID_RE = re.compile(r"^\d{1,64}$")
_POWERED_BY_NEXUSPHP_RE = re.compile(r"\s*-\s*Powered by NexusPHP\s*$", re.IGNORECASE)
_DETAIL_TITLE_RE = re.compile(r'^(?:种子详情|Torrent Details?)\s*["“](.+)["”]$', re.IGNORECASE)


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
    require_valid_torrent_metainfo: bool = False

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

HHCLUB_PROFILE = NexusPhpProfile(
    site_id="hhclub",
    default_base_url="https://hhanclub.net",
    timezone_offset_minutes=8 * 60,
    min_request_interval_seconds=2.0,
)


@dataclass(slots=True)
class _Row:
    cells: list[str] = field(default_factory=list)
    cell_text_parts: list[list[str]] = field(default_factory=list)
    cell_titles: list[list[str]] = field(default_factory=list)
    links: list[tuple[str, str | None, str]] = field(default_factory=list)
    active_cell: int | None = None


@dataclass(slots=True)
class _Card:
    links: list[tuple[str, str | None, str]] = field(default_factory=list)
    field_parts: dict[str, list[str]] = field(default_factory=dict)


class _NexusHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._rows: list[_Row] = []
        self._row_stack: list[_Row] = []
        self._cards: list[_Card] = []
        self._card_stack: list[_Card] = []
        self._card_element_stack: list[tuple[str, str | None, bool]] = []
        self._link_stack: list[tuple[str, str | None, list[str]]] = []
        self._heading_depth = 0
        self._heading_text: list[str] = []
        self._title_depth = 0
        self._title_text: list[str] = []
        self.text_parts: list[str] = []
        self.all_links: list[tuple[str, str | None, str]] = []

    @property
    def rows(self) -> tuple[_Row, ...]:
        return tuple(self._rows)

    @property
    def cards(self) -> tuple[_Card, ...]:
        return tuple(self._cards)

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
        classes = set((attributes.get("class") or "").split())
        starts_card = tag == "div" and "torrent-table-sub-info" in classes
        if starts_card:
            self._card_stack.append(_Card())
        card_field = _card_field(classes) if self._card_stack else None
        self._card_element_stack.append((tag, card_field, starts_card))
        if tag == "tr":
            self._row_stack.append(_Row())
        elif tag == "td" and self._row_stack:
            row = self._row_stack[-1]
            row.cells.append("")
            row.cell_text_parts.append([])
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
            for card in self._card_stack:
                card.links.append(link)
        elif tag == "h1" and self._heading_depth:
            self._heading_depth -= 1
        elif tag == "title" and self._title_depth:
            self._title_depth -= 1

        for index in range(len(self._card_element_stack) - 1, -1, -1):
            if self._card_element_stack[index][0] != tag:
                continue
            closed = self._card_element_stack[index:]
            del self._card_element_stack[index:]
            for _, _, starts_card in reversed(closed):
                if starts_card and self._card_stack:
                    self._cards.append(self._card_stack.pop())
            break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        cleaned = _clean_text(data)
        if cleaned:
            self.text_parts.append(cleaned)
        for row in self._row_stack:
            if row.active_cell is not None:
                row.cells[row.active_cell] += data
                if cleaned:
                    row.cell_text_parts[row.active_cell].append(cleaned)
        if self._link_stack:
            self._link_stack[-1][2].append(data)
        if self._heading_depth:
            self._heading_text.append(data)
        if self._title_depth:
            self._title_text.append(data)
        if self._card_stack:
            for _, field_name, _ in reversed(self._card_element_stack):
                if field_name is not None:
                    self._card_stack[-1].field_parts.setdefault(field_name, []).append(data)
                    break


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
        user_agent: str | None = None,
        browser_emulation_enabled: bool = False,
        proxy_url: str | None = None,
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
        self._user_agent = user_agent.strip() if user_agent else None
        self._browser_emulation_enabled = browser_emulation_enabled
        self._proxy_url = proxy_url
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

    async def fetch_user_profile(self) -> SiteUserProfile:
        index = _parse_html(await self._get_html(self._profile.index_path))
        self._reject_login_page(index)
        authenticated = any(
            _href_path(href).endswith(self._profile.authenticated_href)
            for href, _, _ in index.all_links
        )
        if not authenticated:
            raise SiteAdapterError("SITE_AUTH_FAILED", "NexusPHP Cookie 无效或会话已失效")
        profile_link = _user_profile_link(index.all_links)
        confirmed_from_settings = False
        if profile_link is None:
            parser = _parse_html(await self._get_html(f"/{self._profile.authenticated_href}"))
            self._reject_login_page(parser)
            uid = None
            username = None
            # A public homepage can link to many other accounts. The signed-in
            # user's own settings page may provide a unique ordinary profile
            # link without requiring us to guess an identity from that homepage.
            settings_link = _user_profile_link(parser.all_links)
            if settings_link is not None:
                href, settings_uid, settings_username = settings_link
                try:
                    profile_url = self._same_origin_url(href)
                except SiteAdapterError:
                    # Never follow a foreign link or use its UID for AJAX.
                    pass
                else:
                    parser = _parse_html(await self._get_html_url(profile_url))
                    uid = settings_uid
                    username = settings_username
                    confirmed_from_settings = True
        else:
            href, uid, username = profile_link
            parser = _parse_html(await self._get_html_url(self._same_origin_url(href)))
        self._reject_login_page(parser)
        profile = _parse_nexus_user_profile(
            parser,
            site_id=self._profile.site_id,
            uid=uid,
            username=username,
        )
        if self._profile.site_id == "hhclub" and profile.bonus_per_hour is None:
            # HHClub displays the hourly rate on its independent bonus page.
            # Follow only an explicit same-origin profile link to that known read-only page.
            bonus_href = next(
                (
                    href
                    for href, _, _ in parser.all_links
                    if urlsplit(href).path.lstrip("/") == "mybonus.php"
                    and not urlsplit(href).query
                    and not urlsplit(href).fragment
                ),
                None,
            )
            if bonus_href is not None:
                try:
                    bonus_page = _parse_html(
                        await self._get_html_url(self._same_origin_url(bonus_href))
                    )
                    self._reject_login_page(bonus_page)
                    hourly = _parse_hhclub_hourly_bonus(bonus_page)
                    if hourly is not None:
                        profile = replace(profile, bonus_per_hour=hourly)
                except SiteAdapterError:
                    # Optional profile statistics must not hide the valid main profile.
                    # Never invent a numeric value when the bonus page is inaccessible.
                    pass
        if (
            self._profile.site_id == "hhclub"
            and uid is not None
            and profile.torrents_posted is None
        ):
            publication_href = _own_published_list_href(parser, uid=uid)
            if publication_href is not None:
                try:
                    published_page = await self._get_html_url(
                        self._same_origin_url(publication_href)
                    )
                    self._reject_login_page(_parse_html(published_page))
                    # Only the current user's explicitly linked publication page
                    # may authorize this fixed, read-only, first-page JSON request.
                    if _hhclub_publication_ajax_confirmed(published_page, uid=uid):
                        published_json = await self._get_html(
                            "/getusertorrentlistajax.php",
                            params={"type": "uploaded", "userid": uid, "ajax": "1", "page": "0"},
                        )
                        published_total = _parse_hhclub_published_total(published_json)
                        if published_total is not None:
                            profile = replace(profile, torrents_posted=published_total)
                except SiteAdapterError:
                    # A missing/invalid optional published list must not hide the
                    # authenticated user profile or fabricate a zero total.
                    pass
        if (
            self._profile.site_id == "hdtime"
            and uid is not None
            and (profile.seeding_count is None or profile.seeding_size_bytes is None)
        ):
            # Follow only the logged-in user's explicitly linked seeding summary,
            # never an arbitrary URL or a page belonging to another account.
            seeding_href = _own_seeding_summary_href(parser, uid=uid)
            # HDTime's authenticated settings page can identify the current
            # user's profile even when the homepage has other-user links. The
            # real profile displays a plain "当前做种" field without an action=2
            # anchor; in that narrowly verified case the fixed read-only AJAX
            # endpoint may be used directly for this current account.
            ajax_authorized = (
                seeding_href is None
                and confirmed_from_settings
                and any(_clean_text(part).rstrip(":：") == "当前做种" for part in parser.text_parts)
            )
            summary = None
            if seeding_href is not None:
                try:
                    seeding_url = self._same_origin_url(seeding_href)
                except SiteAdapterError:
                    # An untrusted/cross-origin link must not authorize an AJAX request.
                    pass
                else:
                    ajax_authorized = True
                    try:
                        seeding_page = _parse_html(await self._get_html_url(seeding_url))
                        self._reject_login_page(seeding_page)
                        summary = _parse_explicit_seeding_summary(seeding_page)
                    except SiteAdapterError:
                        # A failed optional page must not mask the main profile.
                        pass
            if ajax_authorized and summary is None:
                try:
                    # HDTime loads the current user's full seeding aggregate
                    # from a known read-only HTML fragment, not the paginated rows.
                    ajax_page = _parse_html(
                        await self._get_html(
                            "/getusertorrentlistajax.php",
                            params={"userid": uid, "type": "seeding"},
                        )
                    )
                    self._reject_login_page(ajax_page)
                    summary = _parse_hdtime_ajax_seeding_summary(ajax_page)
                except SiteAdapterError:
                    pass
            if summary is not None:
                count, size = summary
                profile = replace(
                    profile,
                    # The two numbers describe one aggregate snapshot;
                    # never combine a stale partial profile with a newer total.
                    seeding_count=count,
                    seeding_size_bytes=size,
                )
        return profile

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
            _has_next_page(
                parser,
                query.page,
                search_path=self._profile.search_path,
                site_origin=self._base_url,
            ),
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
        if self._profile.require_valid_torrent_metainfo:
            # Candidate sites must reject HTML or truncated bencode that happens
            # to begin with 'd'. Never include the original payload or tracker
            # announce URL in an adapter error.
            try:
                parse_torrent(content)
            except DomainViolation:
                raise SiteAdapterError(
                    "SITE_INVALID_RESPONSE", "NexusPHP torrent 内容校验未通过"
                ) from None
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

    async def _get_html_url(self, url: str) -> str:
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
            headers = {"Accept": accept, "Cookie": self._cookie}
            if self._user_agent:
                headers["User-Agent"] = self._user_agent
            if self._browser_emulation_enabled:
                headers.update(
                    {
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                        "Referer": f"{self._base_url}/",
                    }
                )
            async with (
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    transport=self._transport,
                    proxy=self._proxy_url,
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
        user_agent: str | None = None,
        browser_emulation_enabled: bool = False,
        proxy_url: str | None = None,
        max_html_bytes: int = _DEFAULT_HTML_LIMIT_BYTES,
        max_torrent_bytes: int = _DEFAULT_TORRENT_LIMIT_BYTES,
    ) -> None:
        super().__init__(
            cookie,
            profile=HDTIME_PROFILE,
            base_url=base_url,
            transport=transport,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            browser_emulation_enabled=browser_emulation_enabled,
            proxy_url=proxy_url,
            max_html_bytes=max_html_bytes,
            max_torrent_bytes=max_torrent_bytes,
        )


class HHClubAdapter(NexusPhpWebAdapter):
    def __init__(
        self,
        cookie: str,
        *,
        base_url: str = HHCLUB_PROFILE.default_base_url,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        user_agent: str | None = None,
        browser_emulation_enabled: bool = False,
        proxy_url: str | None = None,
        max_html_bytes: int = _DEFAULT_HTML_LIMIT_BYTES,
        max_torrent_bytes: int = _DEFAULT_TORRENT_LIMIT_BYTES,
    ) -> None:
        normalized_base_url, origin = _normalize_origin(base_url)
        if origin != ("https", "hhanclub.net", None):
            raise ValueError("HHClub base URL 必须是 https://hhanclub.net")
        super().__init__(
            cookie,
            profile=HHCLUB_PROFILE,
            base_url=normalized_base_url,
            transport=transport,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            browser_emulation_enabled=browser_emulation_enabled,
            proxy_url=proxy_url,
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
        _keep_richer_candidate(best_by_id, torrent_id, len(row.cells), candidate)
    for card in parser.cards:
        details = _details_link(card.links)
        if details is None:
            continue
        torrent_id, title = details
        candidate = normalize_candidate_meta(
            site_id=profile.site_id,
            torrent_id=torrent_id,
            display_name=title,
            total_size=_parse_size(_card_text(card, "size")),
            category=_category(card.links),
            seeders=_parse_nonnegative_int(_card_text(card, "seeders")),
            leechers=_parse_nonnegative_int(_card_text(card, "leechers")),
            external_ids=_external_ids(card.links),
        )
        richness = 100 + sum(bool(_card_text(card, key)) for key in ("size", "seeders", "leechers"))
        _keep_richer_candidate(best_by_id, torrent_id, richness, candidate)
    return tuple(item[1] for item in best_by_id.values())


def _details_link(links: list[tuple[str, str | None, str]]) -> tuple[str, str] | None:
    for href, title, text in links:
        parsed = urlsplit(href)
        if _script_name(parsed.path) != "details.php":
            continue
        values = parse_qs(parsed.query).get("id")
        if not values or _TORRENT_ID_RE.fullmatch(values[0]) is None:
            continue
        display = _clean_text(title or text)
        if display:
            return values[0], display
    return None


def _user_profile_link(
    links: list[tuple[str, str | None, str]],
) -> tuple[str, str, str | None] | None:
    candidate: tuple[str, str, str | None] | None = None
    for href, _, text in links:
        parsed = urlsplit(href)
        if parsed.path.lstrip("/") != "userdetails.php" or parsed.fragment:
            continue
        params = parse_qs(parsed.query)
        if set(params) != {"id"}:
            # Seeding/action links are not evidence of the current user's
            # identity, even when the homepage links to no other user.
            continue
        values = params["id"]
        if not values or len(values) != 1 or not values[0].isdigit():
            continue
        username = _clean_text(text) or None
        if candidate is not None:
            if candidate[1] != values[0]:
                # A homepage may link to other users. Never infer which of
                # multiple IDs belongs to the authenticated account.
                return None
            continue
        candidate = (href, values[0], username)
    return candidate


def _parse_nexus_user_profile(
    parser: _NexusHtmlParser,
    *,
    site_id: str,
    uid: str | None,
    username: str | None,
) -> SiteUserProfile:
    text = "|".join(parser.text_parts)
    uploaded = _profile_size(text, "上传量", "Uploaded")
    downloaded = _profile_size(text, "下载量", "Downloaded")
    ratio = _profile_float(text, "分享率", "Ratio")
    if ratio is None and uploaded is not None and downloaded not in {None, 0}:
        ratio = uploaded / downloaded
    return SiteUserProfile(
        site_id=site_id,
        uid=uid,
        username=username or _profile_text(text, "用户名", "Username", max_length=256),
        user_level=_profile_named_level(parser),
        real_uploaded_bytes=_profile_size(text, "真实上传量", "Real Uploaded"),
        real_downloaded_bytes=_profile_size(text, "真实下载量", "Real Downloaded"),
        uploaded_bytes=uploaded,
        downloaded_bytes=downloaded,
        ratio=ratio,
        torrents_posted=_profile_int(text, "发种数", "发布种子", "发布数", "Torrents Posted"),
        seeding_count=_profile_int(text, "做种数", "当前做种", "正在做种", "Seeding"),
        seeding_size_bytes=_profile_size(text, "做种量", "当前做种量", "Seeding Size"),
        bonus=_profile_float(text, "魔力值", "Bonus"),
        seeding_points=_profile_float(text, "做种积分", "Seeding Points"),
        bonus_per_hour=_profile_float(text, "每小时魔力值", "每小时魔力", "Bonus per hour"),
    )


def _profile_named_level(parser: _NexusHtmlParser) -> str | None:
    # Some sites split official names (e.g. "INSANE" + "USER") across nested
    # elements inside the same grade cell. Do not join unrelated profile fields.
    for row in parser.rows:
        for index, cell in enumerate(row.cells[:-1]):
            if _clean_text(cell).rstrip(":：") not in {
                "等级",
                "等級",
                "用户等级",
                "用戶等級",
                "Class",
            }:
                continue
            parts = row.cell_text_parts[index + 1]
            name = _clean_text(" ".join(parts))
            if name and len(name) <= 256 and not name.isdecimal():
                return name
    text = "|".join(parser.text_parts)
    fallback_name = _profile_text(text, "用户等级", "等级", "等級", "Class", max_length=256)
    return fallback_name if fallback_name is not None and not fallback_name.isdecimal() else None


def _own_seeding_summary_href(parser: _NexusHtmlParser, *, uid: str) -> str | None:
    for href, title, label in parser.all_links:
        if not any(
            key in _clean_text(f"{title or ''} {label}")
            for key in ("当前做种", "目前做種", "目前做种", "正在做种")
        ):
            continue
        parsed = urlsplit(href)
        if parsed.path.lstrip("/") != "userdetails.php" or parsed.fragment:
            continue
        params = parse_qs(parsed.query)
        if set(params) != {"id", "action"}:
            continue
        if params["id"] != [uid] or params["action"] != ["2"]:
            continue
        return href
    return None


def _own_published_list_href(parser: _NexusHtmlParser, *, uid: str) -> str | None:
    candidates: set[str] = set()
    for href, title, label in parser.all_links:
        if not any(
            term in _clean_text(f"{title or ''} {label}") for term in ("发种", "发布", "發布")
        ):
            continue
        parsed = urlsplit(href)
        if parsed.path.lstrip("/") != "userdetails.php" or parsed.fragment:
            continue
        params = parse_qs(parsed.query)
        if set(params) == {"id", "action"} and params["id"] == [uid] and params["action"] == ["1"]:
            candidates.add(href)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _hhclub_publication_ajax_confirmed(page_html: str, *, uid: str) -> bool:
    # Verified on HHClub's own "发布种子" page. The page supplies a GET query
    # with type=uploaded, ajax=1 and a page parameter; do not guess variants.
    return (
        "getusertorrentlistajax.php" in page_html
        and "ajax=1" in page_html
        and "page=" in page_html
        and (f"userid={uid}&ajax=1" in page_html or f"userid={uid}&amp;ajax=1" in page_html)
        and re.search(r"\b[A-Za-z_][A-Za-z_0-9]*\s*\(\s*['\"]uploaded['\"]\s*[,)]", page_html)
        is not None
        and "合计" in page_html
        and "发种数量" in page_html
    )


def _parse_hhclub_published_total(response_text: str) -> int | None:
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    total = payload.get("total_count")
    rows = payload.get("data")
    page_num = payload.get("page_num")
    count = payload.get("count")
    if (
        type(total) is not int
        or total < 0
        or not isinstance(rows, list)
        or len(rows) > total
        or type(page_num) is not int
        or page_num < 0
        or not isinstance(count, str)
        or not count.isascii()
        or not count.isdecimal()
        or int(count) > total
    ):
        return None
    # total_offical_count is a separate class of torrents; never add it to
    # total_count or infer a full total from the first page's data/count.
    return total


def _parse_explicit_seeding_summary(
    parser: _NexusHtmlParser, *, require_leading: bool = False
) -> tuple[int, int] | None:
    # An explicit row-count/total-size pair is distinct from a visible list page,
    # from its pagination bounds, and from incentive-eligible seeding categories.
    content = "|".join(parser.text_parts)
    pattern = re.compile(
        r"(?:^|\|)\s*([0-9][0-9,]*)\s*(?:\|\s*)?条记录\s*(?:\|\s*)?"
        r"(?:[/／｜]\s*(?:\|\s*)?)?"
        r"总大小\s*[:：]?\s*(?:\|\s*)?([0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B)"
        r"(?=\s*(?:\||$))"
    )
    matches = list(pattern.finditer(content))
    if len(matches) != 1 or (require_leading and matches[0].start() != 0):
        return None
    count = _parse_nonnegative_int(matches[0].group(1))
    size = _parse_size(matches[0].group(2))
    return (count, size) if count is not None and size is not None else None


def _parse_hdtime_ajax_seeding_summary(parser: _NexusHtmlParser) -> tuple[int, int] | None:
    # The independently verified AJAX fragment starts with its full aggregate.
    # Its following table, pagination and per-torrent values are not totals.
    leading_text = "|".join(parser.text_parts[:3])
    if not re.match(r"^\s*[0-9][0-9,]*\s*(?:\|\s*)?条记录\s*(?:\|\s*)?", leading_text):
        return None
    return _parse_explicit_seeding_summary(parser, require_leading=True)


def _parse_hhclub_hourly_bonus(parser: _NexusHtmlParser) -> float | None:
    # Avoid using the formula's A/B coefficients or bonus history as the hourly rate.
    # Only the site's explicit current-hour reward sentence is authoritative here.
    pattern = re.compile(
        r"你当前每小时能获取\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*个?(?:积分|魔力值|憨豆)"
    )
    for part in parser.text_parts:
        match = pattern.search(part)
        if match is None:
            continue
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if 0 <= value < float("inf"):
            return value
    return None


def _profile_value(text: str, labels: tuple[str, ...], value_pattern: str) -> str | None:
    for label in labels:
        match = re.search(
            rf"(?:^|\|)\s*{re.escape(label)}\s*[:：]?\s*(?:\|\s*)?({value_pattern})(?=\s*(?:\||$))",
            text,
            re.IGNORECASE,
        )
        if match is not None:
            return _clean_text(match.group(1))
    return None


def _profile_text(text: str, *labels: str, max_length: int) -> str | None:
    value = _profile_value(text, labels, r"[^|]{1,512}")
    if value is None or len(value) > max_length:
        return None
    return value


def _profile_size(text: str, *labels: str) -> int | None:
    value = _profile_value(text, labels, r"[0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B")
    return _parse_size(value or "")


def _profile_int(text: str, *labels: str) -> int | None:
    value = _profile_value(text, labels, r"[0-9][0-9,]*")
    return _parse_nonnegative_int(value or "")


def _profile_float(text: str, *labels: str) -> float | None:
    value = _profile_value(text, labels, r"[0-9]+(?:\.[0-9]+)?")
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if 0 <= parsed < float("inf") else None


def _download_href(links: list[tuple[str, str | None, str]], torrent_id: str) -> str | None:
    for href, _, _ in links:
        parsed = urlsplit(href)
        if _script_name(parsed.path) != "download.php":
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


def _has_next_page(
    parser: _NexusHtmlParser,
    current_page: int,
    *,
    search_path: str,
    site_origin: str,
) -> bool:
    for href, _, _ in parser.all_links:
        parsed = urlsplit(href)
        # Any link on the page might have a "page" argument (comments,
        # profiles, external URLs). Only an actual torrent-search pager can
        # signal that the next search page exists.
        if parsed.path not in ("", search_path, search_path.lstrip("/")):
            continue
        if parsed.scheme or parsed.netloc:
            try:
                if _origin_tuple(parsed) != _origin_tuple(urlsplit(site_origin)):
                    continue
            except ValueError:
                continue
        pages = parse_qs(parsed.query).get("page")
        if pages and len(pages) == 1 and pages[0].isdigit() and int(pages[0]) == current_page:
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


def _card_field(classes: set[str]) -> str | None:
    for class_name, field_name in (
        ("torrent-info-text-name", "name"),
        ("torrent-info-text-size", "size"),
        ("torrent-info-text-added", "added"),
        ("torrent-info-text-seeders", "seeders"),
        ("torrent-info-text-leechers", "leechers"),
    ):
        if class_name in classes:
            return field_name
    return None


def _card_text(card: _Card, field_name: str) -> str:
    return _clean_text(" ".join(card.field_parts.get(field_name, ())))


def _keep_richer_candidate(
    best_by_id: dict[str, tuple[int, CandidateMeta]],
    torrent_id: str,
    richness: int,
    candidate: CandidateMeta,
) -> None:
    current = best_by_id.get(torrent_id)
    if current is None or richness > current[0]:
        best_by_id[torrent_id] = (richness, candidate)


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
        query = parse_qs(urlsplit(href).query)
        values = query.get("cat") or query.get("cat[]")
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
    cleaned = _POWERED_BY_NEXUSPHP_RE.sub("", value)
    parts = [_clean_text(part) for part in cleaned.split("::")]
    meaningful = [part for part in parts if part and "nexusphp" not in part.casefold()]
    if len(meaningful) < 2:
        return None
    candidate = meaningful[-1]
    match = _DETAIL_TITLE_RE.fullmatch(candidate)
    return _clean_text(match.group(1)) if match is not None else candidate


def _validate_torrent_id(value: str) -> str:
    normalized = value.strip()
    if _TORRENT_ID_RE.fullmatch(normalized) is None:
        raise ValueError("NexusPHP torrent ID 必须是有限长度十进制 ID")
    return normalized


def _href_path(value: str) -> str:
    return urlsplit(value).path


def _script_name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1].casefold()


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _content_length(response: httpx2.Response) -> int | None:
    value = response.headers.get("content-length")
    return int(value) if value is not None and value.isdecimal() else None
