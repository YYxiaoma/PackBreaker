from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import SplitResult, parse_qs, urlencode, urljoin, urlsplit

import httpx2

from backend.app.domain.errors import DomainViolation
from backend.app.domain.media_matching import ExternalMediaId, parse_media_name
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
from backend.app.infrastructure.adapters.site_retry_after import retry_after_seconds
from backend.app.infrastructure.torrent_parser import parse_torrent

_logger = logging.getLogger("packbreaker.site_adapter.nexusphp")

_DEFAULT_HTML_LIMIT_BYTES = 5 * 1024 * 1024
_DEFAULT_TORRENT_LIMIT_BYTES = 20 * 1024 * 1024
_DEFAULT_PROFILE_TOTAL_TIMEOUT_SECONDS = 30.0
_IMDB_RE = re.compile(r"/title/(tt\d{5,10})", re.IGNORECASE)
_DOUBAN_RE = re.compile(r"/subject/(\d{3,12})")
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B)\s*$", re.IGNORECASE)
_SIZE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_TORRENT_ID_RE = re.compile(r"^\d{1,64}$")
_KEEPFRDS_CLASS_NAMES = {
    0: "Peasant",
    1: "User",
    2: "Power User",
    3: "Elite User",
    4: "Crazy User",
    5: "Insane User",
    6: "Veteran User",
    7: "Extreme User",
    8: "Ultimate User",
    9: "Nexus Master",
    10: "贵宾",
    11: "养老族",
    12: "发布员",
    13: "总版主",
    14: "管理员",
    15: "维护开发员",
    16: "主管",
}
_POWERED_BY_NEXUSPHP_RE = re.compile(r"\s*-\s*Powered by NexusPHP\s*$", re.IGNORECASE)
_DETAIL_TITLE_RE = re.compile(r'^(?:种子详情|Torrent Details?)\s*["“](.+)["”]$', re.IGNORECASE)


def _decode_nexus_html(raw: bytes) -> str:
    """容忍少量畸形字节，但拒绝明显损坏的 HTML 响应。"""

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        decoded = raw.decode("utf-8", errors="replace")
        replacement_count = decoded.count("\ufffd")
        if replacement_count > max(4, len(raw) // 20_000):
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP HTML 不是有效 UTF-8") from exc
        _logger.warning(
            "NexusPHP HTML 含少量无效 UTF-8 字节，已使用替换字符继续解析",
            extra={"replacement_count": replacement_count},
        )
        return decoded


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
    detail_timeout_seconds: float | None = None

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
        if self.detail_timeout_seconds is not None and not 1 <= self.detail_timeout_seconds <= 120:
            raise ValueError("NexusPHP 详情请求超时必须在 1～120 秒之间")


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
    classes: set[str] = field(default_factory=set)
    table_index: int = 0
    cells: list[str] = field(default_factory=list)
    cell_text_parts: list[list[str]] = field(default_factory=list)
    cell_titles: list[list[str]] = field(default_factory=list)
    cell_attributes: list[list[tuple[str, str, str]]] = field(default_factory=list)
    links: list[tuple[str, str | None, str]] = field(default_factory=list)
    active_cell: int | None = None


@dataclass(slots=True)
class _TextCapture:
    tag: str
    class_name: str
    parts: list[str] = field(default_factory=list)
    depth: int = 1


class _PTTimeSummaryParser(HTMLParser):
    """Capture the first four direct span children of PTTime's #outer summary."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.outer_tag: str | None = None
        self.outer_depth = 0
        self.span_depth = 0
        self.current_span: int | None = None
        self.spans: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.outer_depth == 0 and attributes.get("id") == "outer":
            self.outer_tag = tag
            self.outer_depth = 1
            return
        if self.outer_depth == 0:
            return
        if tag == self.outer_tag:
            self.outer_depth += 1
        if self.outer_depth == 1 and tag == "span" and self.current_span is None:
            self.spans.append([])
            self.current_span = len(self.spans) - 1
            self.span_depth = 1
        elif self.current_span is not None and tag == "span":
            self.span_depth += 1

    def handle_data(self, data: str) -> None:
        if self.outer_depth > 0 and self.current_span is not None:
            self.spans[self.current_span].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current_span is not None and tag == "span":
            self.span_depth -= 1
            if self.span_depth == 0:
                self.current_span = None
        if self.outer_tag is not None and tag == self.outer_tag:
            self.outer_depth -= 1
            if self.outer_depth == 0:
                self.outer_tag = None
                self.current_span = None


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
        self._link_stack: list[tuple[str, str | None, str | None, list[str]]] = []
        self._class_text_captures: list[_TextCapture] = []
        self.class_text_parts: dict[str, list[str]] = {}
        self.audiences_level_classes: list[str] = []
        self.all_link_classes: list[tuple[str, str | None, str]] = []
        self._table_stack: list[int] = []
        self._next_table_index = 0
        self._heading_depth = 0
        self._heading_text: list[str] = []
        self._secondary_heading_depth = 0
        self._secondary_heading_text: list[str] = []
        self._title_depth = 0
        self._title_text: list[str] = []
        self._meta_title_text: list[str] = []
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
    def secondary_heading(self) -> str | None:
        value = _clean_text(" ".join(self._secondary_heading_text))
        return value or None

    @property
    def page_title(self) -> str | None:
        value = _clean_text(" ".join(self._title_text))
        return value or None

    @property
    def meta_title(self) -> str | None:
        value = _clean_text(" ".join(self._meta_title_text))
        return value or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        for capture in self._class_text_captures:
            if capture.tag == tag:
                capture.depth += 1
        profile_capture_classes = (
            "row-span-4",
            "ud-class",
            "site-userbar__compact-metric--uploaded",
            "site-userbar__compact-metric--downloaded",
            "site-userbar__compact-metric--ratio",
            "site-userbar__compact-metric--bonus",
            "site-userbar__compact-metric--seeding-bonus",
            "site-userbar__compact-metric-inline-link--seeding",
            "site-userbar__compact-metric--hr",
            "mybonus-side__rate",
            "torrent-info-text-size",
        )
        for captured_class in profile_capture_classes:
            if captured_class in classes:
                self._class_text_captures.append(_TextCapture(tag=tag, class_name=captured_class))
        for class_name in classes:
            folded_class = class_name.casefold()
            if "title" in folded_class or folded_class in {
                "torrentname",
                "torrent_name",
                "torrent-name",
            }:
                self._class_text_captures.append(_TextCapture(tag=tag, class_name=class_name))
        if tag == "b" and any(
            capture.class_name == "ud-class" for capture in self._class_text_captures
        ):
            self.audiences_level_classes.extend(
                class_name[: -len("_Name")].replace("_", " ")
                for class_name in classes
                if class_name.endswith("_Name") and len(class_name) > len("_Name")
            )
        if tag == "table":
            self._next_table_index += 1
            self._table_stack.append(self._next_table_index)
        starts_card = tag == "div" and "torrent-table-sub-info" in classes
        if starts_card:
            self._card_stack.append(_Card())
        card_field = _card_field(classes) if self._card_stack else None
        self._card_element_stack.append((tag, card_field, starts_card))
        if tag == "tr":
            self._row_stack.append(
                _Row(classes=classes, table_index=self._table_stack[-1] if self._table_stack else 0)
            )
        elif tag == "td" and self._row_stack:
            row = self._row_stack[-1]
            row.cells.append("")
            row.cell_text_parts.append([])
            row.cell_titles.append([])
            row.cell_attributes.append([])
            row.active_cell = len(row.cells) - 1
        elif tag in {"span", "time"} and self._row_stack:
            # NexusPHP derivatives may place the absolute publish timestamp
            # on a <time title="..."> element, with relative text inside.
            # Like span titles, retain this metadata ONLY in the active row
            # cell; the date parser still validates its strict format.
            title = attributes.get("title")
            row = self._row_stack[-1]
            if title and row.active_cell is not None:
                row.cell_titles[row.active_cell].append(title)
        elif tag == "a":
            href = attributes.get("href") or ""
            self._link_stack.append((href, attributes.get("title"), attributes.get("class"), []))
        elif tag == "h1":
            self._heading_depth += 1
        elif tag in {"h2", "h3"}:
            self._secondary_heading_depth += 1
        elif tag == "meta":
            meta_name = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).casefold()
            meta_content = attributes.get("content")
            if meta_name in {"og:title", "twitter:title", "title"} and meta_content:
                self._meta_title_text.append(meta_content)
        elif tag == "title":
            self._title_depth += 1
        if self._row_stack:
            row = self._row_stack[-1]
            if row.active_cell is not None:
                for attribute_name in ("title", "alt", "aria-label"):
                    attribute_value = attributes.get(attribute_name)
                    if attribute_value:
                        row.cell_attributes[row.active_cell].append(
                            (tag, attribute_name, attribute_value)
                        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row_stack:
            self._rows.append(self._row_stack.pop())
        elif tag == "td" and self._row_stack:
            self._row_stack[-1].active_cell = None
        elif tag == "a" and self._link_stack:
            href, title, class_name, parts = self._link_stack.pop()
            label = _clean_text(" ".join(parts))
            link = (href, title, label)
            self.all_links.append(link)
            self.all_link_classes.append((href, class_name, label))
            for row in self._row_stack:
                row.links.append(link)
            for card in self._card_stack:
                card.links.append(link)
        elif tag == "h1" and self._heading_depth:
            self._heading_depth -= 1
        elif tag in {"h2", "h3"} and self._secondary_heading_depth:
            self._secondary_heading_depth -= 1
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
        if tag == "table" and self._table_stack:
            self._table_stack.pop()
        remaining_captures: list[_TextCapture] = []
        for capture in self._class_text_captures:
            if capture.tag == tag:
                capture.depth -= 1
            if capture.depth <= 0:
                captured_text = _clean_text(" ".join(capture.parts))
                if captured_text:
                    self.class_text_parts.setdefault(capture.class_name, []).append(captured_text)
            else:
                remaining_captures.append(capture)
        self._class_text_captures = remaining_captures

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
            self._link_stack[-1][3].append(data)
        for capture in self._class_text_captures:
            capture.parts.append(data)
        if self._heading_depth:
            self._heading_text.append(data)
        elif self._secondary_heading_depth:
            self._secondary_heading_text.append(data)
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
        details_timeout_seconds: float | None = None,
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
        if details_timeout_seconds is not None and not 0 < details_timeout_seconds <= 3600:
            raise ValueError("NexusPHP 详情专用超时必须在 0～3600 秒范围内")
        self._details_timeout_seconds = details_timeout_seconds or timeout_seconds
        self._user_agent = user_agent.strip() if user_agent else None
        self._browser_emulation_enabled = browser_emulation_enabled
        self._proxy_url = proxy_url
        self._max_html_bytes = max_html_bytes
        self._max_torrent_bytes = max_torrent_bytes

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=True,
            # HDTime's current search form exposes IMDb-link search, but no
            # Douban-link search mode. Do not issue a misleading Douban-ID query.
            supports_douban_id=self._profile.site_id != "hdtime",
            supports_category=True,
            supports_pagination=True,
            supports_detail_file_list=False,
            requires_download_token=False,
            min_request_interval_seconds=self._profile.min_request_interval_seconds,
        )

    async def test_connection(self) -> SiteConnectionResult:
        parser = _parse_html(await self._get_html(self._profile.index_path))
        self._reject_rate_limit_page(parser)
        if not any(
            _href_path(href).endswith(self._profile.authenticated_href)
            for href, _, _ in parser.all_links
        ):
            raise SiteAdapterError("SITE_AUTH_FAILED", "NexusPHP Cookie 无效或会话已失效")
        return SiteConnectionResult(self._profile.site_id)

    async def fetch_user_profile(self) -> SiteUserProfile:
        site_key = self._profile.site_id.replace("_", "").lower()
        # PT-Depiler uses a 30-second default for user-info requests. Keep the
        # three sites with confirmed slow profile/stat endpoints bounded but
        # aligned with that behavior instead of dropping optional fields at 15s.
        profile_timeout_seconds = (
            max(self._timeout_seconds, _DEFAULT_PROFILE_TOTAL_TIMEOUT_SECONDS)
            if site_key in {"ubits", "audiences", "springsunday"}
            else None
        )

        async def get_profile_html(
            path: str,
            *,
            params: dict[str, str] | None = None,
            headers: dict[str, str] | None = None,
        ) -> str:
            if profile_timeout_seconds is None:
                return await self._get_html(path, params=params, headers=headers)
            try:
                # HTTPX read timeouts are inactivity limits, not a total deadline.
                # PT-Depiler's Axios timeout is total-request based; mirror that
                # bounded behavior so a slow chunked response cannot hang profile sync.
                return await asyncio.wait_for(
                    self._get_html(
                        path,
                        params=params,
                        timeout_seconds=profile_timeout_seconds,
                        headers=headers,
                    ),
                    timeout=profile_timeout_seconds,
                )
            except TimeoutError as exc:
                raise SiteAdapterError(
                    "SITE_TIMEOUT",
                    "NexusPHP 用户资料请求超过总耗时上限",
                    retryable=True,
                ) from exc

        index = _parse_html(await get_profile_html(self._profile.index_path))
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
            parser = _parse_html(await get_profile_html(f"/{self._profile.authenticated_href}"))
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
                    if urlsplit(profile_url).path.lstrip("/") != "userdetails.php":
                        raise SiteAdapterError(
                            "SITE_REQUEST_POLICY_VIOLATION",
                            "站点用户资料链接路径不受支持",
                        )
                    # Validate the original link's origin first, then request the
                    # fixed same-origin profile endpoint with only the numeric id.
                    parser = _parse_html(
                        await get_profile_html("/userdetails.php", params={"id": settings_uid})
                    )
                    uid = settings_uid
                    username = settings_username
                    confirmed_from_settings = True
        else:
            href, uid, username = profile_link
            profile_url = self._same_origin_url(href)
            if urlsplit(profile_url).path.lstrip("/") != "userdetails.php":
                raise SiteAdapterError(
                    "SITE_REQUEST_POLICY_VIOLATION", "站点用户资料链接路径不受支持"
                )
            # Do not forward auxiliary query parameters from the page's link.
            parser = _parse_html(await get_profile_html("/userdetails.php", params={"id": uid}))
        self._reject_login_page(parser)
        profile = _parse_nexus_user_profile(
            parser,
            site_id=self._profile.site_id,
            uid=uid,
            username=username,
            timezone_offset_minutes=self._profile.timezone_offset_minutes,
        )
        numeric_uid = uid is not None and re.fullmatch(r"[0-9]{1,64}", uid) is not None

        if site_key == "keepfrds":
            # KeepFrds removed the old AJAX user-torrent endpoint. Its JSON API is
            # the authoritative source for class, byte totals, published and seeding totals.
            try:
                api_raw = await self._get_bytes(
                    f"{self._base_url}/api/userdetails.php",
                    limit=self._max_html_bytes,
                    accept="application/json",
                )
                profile = _parse_keepfrds_api_profile(
                    profile,
                    api_raw.decode("utf-8"),
                    expected_uid=uid,
                )
            except (SiteAdapterError, UnicodeDecodeError) as exc:
                error_code = (
                    exc.code if isinstance(exc, SiteAdapterError) else "SITE_INVALID_RESPONSE"
                )
                _logger.warning(
                    "KeepFrds 用户统计 API 读取失败，保留网页可解析字段 error_code=%s",
                    error_code,
                )

        if site_key == "u2" and numeric_uid and profile.bonus_per_hour is None:
            try:
                if self._transport is None and self._profile.min_request_interval_seconds > 0:
                    await asyncio.sleep(self._profile.min_request_interval_seconds)
                hourly_html = await self._get_html("/mprecent.php", params={"user": uid or ""})
                hourly_bonus = _parse_u2_bonus_per_hour(hourly_html)
                if hourly_bonus is not None:
                    profile = replace(profile, bonus_per_hour=hourly_bonus)
            except SiteAdapterError as exc:
                _logger.warning("U2 每小时 UCoin 统计读取失败 error_code=%s", exc.code)

        if site_key == "hhclub" and profile.bonus_per_hour is None:
            hourly_bonus = _parse_hhclub_profile_bonus_per_hour(parser)
            if hourly_bonus is not None:
                profile = replace(profile, bonus_per_hour=hourly_bonus)

        if site_key in {"ubits", "audiences"} and profile.bonus_per_hour is None:
            # PT-Depiler's default user-info flow reads hourly bonus from
            # /mybonus.php. Audiences has a dedicated compact-rate selector;
            # UBits uses the generic "currently getting" sentence.
            try:
                bonus_page = _parse_html(await get_profile_html("/mybonus.php"))
                self._reject_login_page(bonus_page)
                hourly_bonus = _parse_nexus_bonus_per_hour(
                    bonus_page,
                    class_name="mybonus-side__rate" if site_key == "audiences" else None,
                )
                if hourly_bonus is not None:
                    profile = replace(profile, bonus_per_hour=hourly_bonus)
            except SiteAdapterError as exc:
                _logger.warning("%s 每小时收益读取失败 error_code=%s", site_key, exc.code)

        if site_key == "springsunday" and profile.bonus_per_hour is None:
            hourly_bonus = _parse_springsunday_bonus_per_hour(parser)
            if hourly_bonus is not None:
                profile = replace(profile, bonus_per_hour=hourly_bonus)

        if site_key == "pterclub" and profile.seeding_count is None:
            header_seeding_count = _parse_pter_seeding_count(parser)
            if header_seeding_count is not None:
                profile = replace(profile, seeding_count=header_seeding_count)

        # User-profile fields and full-list summaries are optional. A slow or retired
        # auxiliary endpoint must not cause the reliability wrapper to retry the entire
        # homepage + user-details flow three times and trip the site's circuit breaker.
        # Preserve authentication errors from the primary profile pages, but record
        # auxiliary failures by safe error code only and return the fields already parsed.
        if numeric_uid and site_key not in {"hdtime", "keepfrds"}:
            if profile.seeding_count is None or profile.seeding_size_bytes is None:
                if self._transport is None and self._profile.min_request_interval_seconds > 0:
                    await asyncio.sleep(self._profile.min_request_interval_seconds)
                seeding_path = (
                    "/getusertorrentlist.php"
                    if site_key == "pttime"
                    else "/getusertorrentlistajax.php"
                )
                request_headers = (
                    {"Referer": f"{self._base_url}/userdetails.php"}
                    if site_key == "audiences"
                    else None
                )
                try:
                    seeding_html = await get_profile_html(
                        seeding_path,
                        params={"userid": uid or "", "type": "seeding"},
                        headers=request_headers,
                    )
                    seeding_parser = _parse_html(seeding_html)
                    self._reject_login_page(seeding_parser)
                    if site_key == "pttime":
                        seeding_summary = _parse_pttime_seeding_summary(seeding_html)
                    elif site_key == "u2":
                        seeding_summary = _parse_u2_ajax_seeding_summary(seeding_html)
                    elif site_key == "audiences":
                        seeding_summary = _parse_audiences_seeding_summary(seeding_parser)
                    else:
                        seeding_summary = _parse_nexus_ajax_seeding_summary(
                            seeding_html, seeding_parser, spring_sunday=site_key == "springsunday"
                        )
                    if seeding_summary is not None:
                        profile = replace(
                            profile,
                            # Preserve an explicitly parsed profile/header count. The
                            # summary endpoint only fills missing fields; this matches
                            # PT-depiler's selector-first behavior and avoids replacing
                            # a current-seeding count with a different list-total value.
                            seeding_count=(
                                profile.seeding_count
                                if profile.seeding_count is not None
                                else seeding_summary[0]
                            ),
                            seeding_size_bytes=(
                                profile.seeding_size_bytes
                                if profile.seeding_size_bytes is not None
                                else seeding_summary[1]
                            ),
                        )
                except SiteAdapterError as exc:
                    _logger.warning(
                        "站点做种汇总读取失败 site_id=%s error_code=%s",
                        site_key,
                        exc.code,
                    )

            if site_key == "pterclub" and profile.username:
                # PT-depiler computes PTerClub's published total from two reviewed
                # torrent-search states; the generic user-torrent AJAX count is not equivalent.
                try:
                    pter_published_total = 0
                    for check_state in ("prechecked", "checked"):
                        if (
                            self._transport is None
                            and self._profile.min_request_interval_seconds > 0
                        ):
                            await asyncio.sleep(self._profile.min_request_interval_seconds)
                        uploaded_html = await self._get_html(
                            "/torrents.php",
                            params={
                                "incldead": "1",
                                "spstate": "0",
                                "inclbookmarked": "0",
                                "check": check_state,
                                "search": profile.username,
                                "search_area": "3",
                                "search_mode": "3",
                            },
                        )
                        uploaded_parser = _parse_html(uploaded_html)
                        self._reject_login_page(uploaded_parser)
                        count = _parse_pter_uploaded_total(uploaded_html)
                        if count is None:
                            raise SiteAdapterError(
                                "SITE_INVALID_RESPONSE", "PTerClub 发种统计页缺少可识别的分页总数"
                            )
                        pter_published_total += count
                    profile = replace(profile, torrents_posted=pter_published_total)
                except SiteAdapterError as exc:
                    _logger.warning("PTerClub 发种统计读取失败 error_code=%s", exc.code)
            elif site_key != "hhclub" and profile.torrents_posted is None:
                if self._transport is None and self._profile.min_request_interval_seconds > 0:
                    await asyncio.sleep(self._profile.min_request_interval_seconds)
                uploaded_path = (
                    "/getusertorrentlist.php"
                    if site_key == "pttime"
                    else "/getusertorrentlistajax.php"
                )
                request_headers = (
                    {"Referer": f"{self._base_url}/userdetails.php"}
                    if site_key == "audiences"
                    else None
                )
                try:
                    uploaded_html = await get_profile_html(
                        uploaded_path,
                        params={"userid": uid or "", "type": "uploaded"},
                        headers=request_headers,
                    )
                    uploaded_parser = _parse_html(uploaded_html)
                    self._reject_login_page(uploaded_parser)
                    published_count = _parse_nexus_ajax_total_count(uploaded_html, uploaded_parser)
                    if published_count is not None:
                        profile = replace(profile, torrents_posted=published_count)
                except SiteAdapterError as exc:
                    _logger.warning(
                        "站点发种统计读取失败 site_id=%s error_code=%s",
                        site_key,
                        exc.code,
                    )
        if site_key == "hdtime" and numeric_uid and profile.torrents_posted is None:
            # PT-Depiler reads NexusPHP publication totals from the current
            # user's uploaded-torrent list. For HDTime, only request that
            # fixed read-only summary after the authenticated profile explicitly
            # links to this same user's publication list (action=1).
            published_href = _own_published_list_href(parser, uid=uid or "")
            if published_href is not None:
                try:
                    published_url = self._same_origin_url(published_href)
                    if urlsplit(published_url).path.lstrip("/") != "userdetails.php":
                        raise SiteAdapterError(
                            "SITE_REQUEST_POLICY_VIOLATION",
                            "HDTime 发种统计链接路径不受支持",
                        )
                    if self._transport is None and self._profile.min_request_interval_seconds > 0:
                        await asyncio.sleep(self._profile.min_request_interval_seconds)
                    uploaded_html = await get_profile_html(
                        "/getusertorrentlistajax.php",
                        params={"userid": uid or "", "type": "uploaded"},
                    )
                    uploaded_parser = _parse_html(uploaded_html)
                    self._reject_login_page(uploaded_parser)
                    published_count = _parse_nexus_ajax_total_count(uploaded_html, uploaded_parser)
                    if published_count is not None:
                        profile = replace(profile, torrents_posted=published_count)
                except SiteAdapterError as exc:
                    _logger.warning("HDTime 发种统计读取失败 error_code=%s", exc.code)

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
        # HDTime's live form maps area 4 to IMDb links; area 0 is title search.
        # Sending an IMDb ID as a title returns an empty-looking page without an
        # error, which previously turned a valid search into a false no-match.
        search_area = (
            "4"
            if self._profile.site_id == "hdtime"
            and any(item.namespace == "imdb" for item in query.external_ids)
            else "0"
        )
        params = {
            "search": _search_value(query),
            "incldead": "1",
            "spstate": "0",
            "search_area": search_area,
            "search_mode": "0",
            "sort": _sort_value(query.sort),
            "type": "desc",
            "notnewword": "1",
            "page": str(query.page - 1),
        }
        parser = _parse_html(await self._get_html(self._profile.search_path, params=params))
        self._reject_rate_limit_page(parser)
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
            await self._get_html(
                self._profile.details_path,
                params={"id": normalized_id},
                timeout_seconds=self._details_timeout_seconds,
            )
        )
        self._reject_rate_limit_page(parser)
        self._reject_login_page(parser)
        for candidate in _parse_search_rows(parser, self._profile):
            if candidate.torrent_id == normalized_id:
                return TorrentDetails(candidate)
        display_name = parser.heading or _page_title_candidate(parser.page_title)
        if display_name is None:
            download_href = _download_href(parser.all_links, normalized_id)
            if download_href is not None:
                title_candidates: list[str] = []
                if parser.secondary_heading:
                    title_candidates.append(parser.secondary_heading)
                if parser.meta_title:
                    title_candidates.append(parser.meta_title)
                title_like_classes = sorted(
                    (
                        class_name
                        for class_name in parser.class_text_parts
                        if "title" in class_name.casefold()
                        or class_name.casefold() in {"torrentname", "torrent_name", "torrent-name"}
                    ),
                    key=lambda name: (
                        0
                        if name.casefold()
                        in {
                            "torrentname",
                            "torrent_name",
                            "torrent-name",
                            "torrent-title",
                            "torrent_title",
                        }
                        else 1
                        if "torrent" in name.casefold()
                        else 2
                        if "detail" in name.casefold()
                        else 3,
                        name.casefold(),
                    ),
                )
                for class_name in title_like_classes:
                    title_candidates.extend(parser.class_text_parts[class_name])
                generic_titles = {
                    "hdtime",
                    "hdt",
                    "hdtorrents",
                    "torrent",
                    "torrent details",
                    "details",
                    "种子详情",
                    "详情",
                    "login",
                    "log in",
                    "登录",
                    "首页",
                    "home",
                }
                for raw_title in title_candidates:
                    cleaned_title = _clean_text(raw_title)
                    if not cleaned_title:
                        continue
                    candidate_title = _page_title_candidate(cleaned_title) or cleaned_title
                    if candidate_title.casefold() in generic_titles:
                        continue
                    if _DETAIL_TITLE_RE.fullmatch(candidate_title):
                        continue
                    display_name = candidate_title
                    break
        if display_name is None:
            title_like_classes = sorted(
                class_name
                for class_name in parser.class_text_parts
                if "title" in class_name.casefold()
                or class_name.casefold() in {"torrentname", "torrent_name", "torrent-name"}
            )[:12]
            _logger.warning(
                "NexusPHP detail page title could not be parsed",
                extra={
                    "site_id": self._profile.site_id,
                    "torrent_id": normalized_id,
                    "page_title": parser.page_title,
                    "heading": parser.heading,
                    "secondary_heading": parser.secondary_heading,
                    "meta_title": parser.meta_title,
                    "title_like_classes": title_like_classes,
                    "link_count": len(parser.all_links),
                    "has_exact_download_link": _download_href(parser.all_links, normalized_id)
                    is not None,
                },
            )
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "NexusPHP 详情页缺少可识别标题")
        return TorrentDetails(
            normalize_candidate_meta(
                site_id=self._profile.site_id,
                torrent_id=normalized_id,
                display_name=display_name,
                total_size=_explicit_detail_size(parser),
                external_ids=_detail_external_ids(parser.all_links, display_name),
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

    async def _get_html(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        raw = await self._get_bytes(
            url,
            limit=self._max_html_bytes,
            accept="text/html",
            timeout_seconds=timeout_seconds,
            extra_headers=headers,
        )
        return _decode_nexus_html(raw)

    async def _get_html_url(self, url: str, *, timeout_seconds: float | None = None) -> str:
        raw = await self._get_bytes(
            url,
            limit=self._max_html_bytes,
            accept="text/html",
            timeout_seconds=timeout_seconds,
        )
        return _decode_nexus_html(raw)

    async def _get_bytes(
        self,
        url: str,
        *,
        limit: int,
        accept: str = "application/x-bittorrent",
        timeout_seconds: float | None = None,
        extra_headers: dict[str, str] | None = None,
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
            for header_name, header_value in (extra_headers or {}).items():
                # Site-specific profile requests may need a same-origin Referer.
                # Do not permit arbitrary caller-supplied headers at this boundary.
                if header_name.casefold() != "referer":
                    raise SiteAdapterError(
                        "SITE_REQUEST_POLICY_VIOLATION", "NexusPHP 仅允许站内 Referer 覆盖"
                    )
                self._assert_same_origin(header_value)
                headers["Referer"] = header_value
            async with (
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=timeout_seconds
                    if timeout_seconds is not None
                    else self._timeout_seconds,
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
        except httpx2.TimeoutException as exc:
            # Preserve the distinction required by the match-timeout state
            # machine; a slow Tracker response is not a network outage.
            raise SiteAdapterError("SITE_TIMEOUT", "NexusPHP 站点请求超时", retryable=True) from exc
        except httpx2.NetworkError as exc:
            raise SiteAdapterError(
                "SITE_UNAVAILABLE", "NexusPHP 网络连接失败", retryable=True
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
            raise SiteAdapterError(
                "SITE_RATE_LIMITED",
                "NexusPHP 请求达到限流",
                retryable=True,
                retry_after_seconds=retry_after_seconds(response),
            )
        if response.status_code >= 500:
            raise SiteAdapterError("SITE_UNAVAILABLE", "NexusPHP 站点暂时不可用", retryable=True)
        if response.status_code != 200:
            raise SiteAdapterError("SITE_HTTP_ERROR", "NexusPHP 返回异常状态")

    def _reject_rate_limit_page(self, parser: _NexusHtmlParser) -> None:
        visible_text = " ".join(
            value
            for value in (
                parser.page_title,
                parser.heading,
                parser.secondary_heading,
                *parser.text_parts,
            )
            if isinstance(value, str) and value
        ).casefold()
        if any(
            marker in visible_text
            for marker in (
                "访问过于频繁",
                "请求过于频繁",
                "操作过于频繁",
                "too many requests",
                "rate limit exceeded",
            )
        ):
            raise SiteAdapterError(
                "SITE_RATE_LIMITED",
                "NexusPHP 请求过于频繁，请稍后重试",
                retryable=True,
                retry_after_seconds=60.0,
            )

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


def _parse_size_text(value: str) -> int | None:
    direct = _parse_size(value)
    if direct is not None:
        return direct
    matches = tuple(_SIZE_TOKEN_RE.finditer(value))
    if len(matches) != 1:
        return None
    return _parse_size(matches[0].group(0))


def _parse_row_size(row: _Row, profile: NexusPhpProfile) -> int | None:
    """Use the configured size column first, then a unique explicit size cell.

    Some NexusPHP skins append columns after the torrent-size column. In that
    layout, fixed offsets shift to seeders/status and silently drop size evidence.
    The fallback is deliberately row-local and accepts only one unambiguous size.
    """
    configured = _parse_size(_cell(row, profile.size_cell_from_end))
    if configured is not None:
        return configured

    values: list[int] = []
    for index, cell in enumerate(row.cells):
        cell_sizes: set[int] = set()
        parsed_cell = _parse_size_text(cell)
        if parsed_cell is not None:
            cell_sizes.add(parsed_cell)
        if index < len(row.cell_attributes):
            for _tag, attribute, raw_value in row.cell_attributes[index]:
                if attribute not in {"title", "aria-label", "alt"}:
                    continue
                parsed_attribute = _parse_size_text(raw_value)
                if parsed_attribute is not None:
                    cell_sizes.add(parsed_attribute)
        if len(cell_sizes) == 1:
            values.append(next(iter(cell_sizes)))

    return values[0] if len(values) == 1 else None


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
            total_size=_parse_row_size(row, profile),
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


def _explicit_detail_size(parser: _NexusHtmlParser) -> int | None:
    """Accept only unambiguous torrent-size labels or the dedicated size class.

    A details page often has no search-result row. Do not infer a torrent size
    from free text, user totals, sibling torrents or the page's other numbers.
    """
    sizes: set[int] = {
        parsed
        for value in parser.class_text_parts.get("torrent-info-text-size", [])
        if (parsed := _parse_size_text(value)) is not None
    }
    if len(sizes) > 1:
        return None
    for row in parser.rows:
        if len(row.cells) != 2:
            continue
        label = _clean_text(row.cells[0]).rstrip(":：").casefold()
        if label not in {"大小", "文件大小", "种子大小", "体积", "size", "torrent size"}:
            continue
        value = _parse_size(row.cells[1])
        if value is None or value <= 0:
            return None
        sizes.add(value)
        if len(sizes) > 1:
            return None
    return next(iter(sizes)) if sizes else None


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
        if set(params) not in ({"id"}, {"id", "hash"}):
            # UBits decorates the current user's profile link with a `hash`
            # parameter. Accept only that known decoration and never forward it.
            # Other query parameters can identify seeding/action links, not the user.
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
        # Preserve the original URL until the caller validates its origin. The
        # caller then rebuilds the fixed profile URL with only this numeric id.
        candidate = (href, values[0], username)
    return candidate


def _parse_nexus_user_profile(
    parser: _NexusHtmlParser,
    *,
    site_id: str,
    uid: str | None,
    username: str | None,
    timezone_offset_minutes: int,
) -> SiteUserProfile:
    text = "|".join(parser.text_parts)
    site_key = site_id.replace("_", "").lower()
    hnr_link_count, hnr_link_status = _profile_hnr(parser)
    uploaded = _profile_size_from_parser(parser, text, "上传量", "上傳量", "Uploaded")
    downloaded = _profile_size_from_parser(parser, text, "下载量", "下載量", "Downloaded")
    ratio = _profile_float_from_parser(parser, text, "分享率", "Ratio")
    if ratio is None and uploaded is not None and downloaded not in {None, 0}:
        ratio = uploaded / downloaded
    hnr_count = _profile_int_from_parser(
        parser,
        text,
        "H&R 次数",
        "H&R次数",
        "HNR 次数",
        "HNR次数",
        "H&R Count",
    )
    if hnr_count is None:
        hnr_count = hnr_link_count
    bonus_labels: tuple[str, ...]
    if site_key == "audiences":
        bonus_labels = ("爆米花", "Karma Points", "积分", "茉莉", "魔力值", "Bonus")
    elif site_key == "springsunday":
        bonus_labels = ("积分", "茉莉", "魔力值", "Bonus")
    elif site_key == "u2":
        bonus_labels = ("UCoin", "U币", "Bonus")
    elif site_key == "ubits":
        bonus_labels = ("U币", "UBits Coin", "魔力值", "Bonus")
    elif site_key == "hhclub":
        bonus_labels = ("憨豆", "魔力值", "Bonus")
    else:
        bonus_labels = ("魔力值", "Bonus")

    seeding_count = _profile_int_from_parser(
        parser, text, "做种数", "当前做种", "正在做种", "Seeding"
    )
    bonus = _profile_float_from_parser(parser, text, *bonus_labels)
    seeding_points = _profile_float_from_parser(
        parser,
        text,
        "做种积分",
        "做種積分",
        "保种积分",
        "保種積分",
        "Seed points",
        "Seeding Points",
    )
    bonus_per_hour = _profile_float_from_parser(
        parser,
        text,
        "每小时魔力值",
        "每小时魔力",
        "每小时憨豆",
        "Bonus per hour",
    )
    if site_key == "audiences":
        uploaded_metric = _profile_class_size(parser, "site-userbar__compact-metric--uploaded")
        downloaded_metric = _profile_class_size(parser, "site-userbar__compact-metric--downloaded")
        ratio_metric = _profile_class_float(parser, "site-userbar__compact-metric--ratio")
        bonus_metric = _profile_class_float(parser, "site-userbar__compact-metric--bonus")
        seeding_points_metric = _profile_class_float(
            parser, "site-userbar__compact-metric--seeding-bonus"
        )
        seeding_count_metric = _profile_class_int(
            parser, "site-userbar__compact-metric-inline-link--seeding"
        )
        bonus_per_hour_metric = _profile_class_float(parser, "mybonus-side__rate")
        if uploaded_metric is not None:
            uploaded = uploaded_metric
        if downloaded_metric is not None:
            downloaded = downloaded_metric
        if ratio_metric is not None:
            ratio = ratio_metric
        if bonus_metric is not None:
            bonus = bonus_metric
        if seeding_points_metric is not None:
            seeding_points = seeding_points_metric
        if seeding_count_metric is not None:
            seeding_count = seeding_count_metric
        if bonus_per_hour_metric is not None:
            bonus_per_hour = bonus_per_hour_metric
        if ratio is None and uploaded is not None and downloaded not in {None, 0}:
            ratio = uploaded / downloaded

    return SiteUserProfile(
        site_id=site_id,
        uid=uid,
        username=username or _profile_text(text, "用户名", "Username", max_length=256),
        user_level=_profile_named_level(parser, site_id=site_id),
        real_uploaded_bytes=_profile_size_from_parser(
            parser,
            text,
            "真实上传量",
            "实际上传量",
            "真實上傳量",
            "實際上傳量",
            "Real Uploaded",
            "Actual Uploaded",
        ),
        real_downloaded_bytes=_profile_size_from_parser(
            parser,
            text,
            "真实下载量",
            "实际下载量",
            "真實下載量",
            "實際下載量",
            "Real Downloaded",
            "Actual Downloaded",
        ),
        uploaded_bytes=uploaded,
        downloaded_bytes=downloaded,
        ratio=ratio,
        torrents_posted=_profile_int_from_parser(
            parser, text, "发种数", "发布种子", "发布数", "Torrents Posted"
        ),
        seeding_count=seeding_count,
        seeding_size_bytes=_profile_size_from_parser(
            parser, text, "做种量", "做种大小", "做種大小", "当前做种量", "Seeding Size"
        ),
        bonus=bonus,
        seeding_points=seeding_points,
        bonus_per_hour=bonus_per_hour,
        join_time=_profile_datetime(
            text,
            "加入日期",
            "加入时间",
            "入站时间",
            "注册时间",
            "注册日期",
            "Join Time",
            "Registered",
            timezone_offset_minutes=timezone_offset_minutes,
        ),
        last_access_at=_profile_datetime(
            text,
            "最近动向",
            "最近访问时间",
            "最后访问时间",
            "最近访问",
            "Last Access At",
            timezone_offset_minutes=timezone_offset_minutes,
        ),
        hnr_status=_profile_text(
            text,
            "H&R 状态",
            "H&R状态",
            "HNR 状态",
            "HNR状态",
            "H&R Status",
            max_length=64,
        )
        or hnr_link_status,
        hnr_count=hnr_count,
        message_count=_profile_int_from_parser(
            parser,
            text,
            "未读消息",
            "未读消息数",
            "未读消息数量",
            "消息数",
            "Message Count",
        ),
    )


def _profile_hnr(parser: _NexusHtmlParser) -> tuple[int | None, str | None]:
    for href, title, label in reversed(parser.all_links):
        parsed = urlsplit(href)
        if parsed.path.lstrip("/") != "myhr.php":
            continue
        value = _clean_text(f"{title or ''} {label}")
        numbers = [int(item) for item in re.findall(r"\d+", value)]
        if not numbers:
            continue
        unsatisfied = numbers[1] if len(numbers) >= 2 else numbers[0]
        return unsatisfied, "正常" if unsatisfied == 0 else "异常"
    return None, None


def _normalize_level_candidate(value: str) -> str | None:
    candidate = _clean_text(value).strip(" :：")
    for guidance_marker in ("🩺校验等级", "校验等级", "等级参考", "加群参考", "规则&教程"):
        if guidance_marker in candidate:
            candidate = candidate.split(guidance_marker, 1)[0].strip(" ：:·-—")
    if not candidate or len(candidate) > 256 or candidate.isdecimal():
        return None
    folded = candidate.casefold()
    if folded.startswith(("next class", "next level", "下一级", "下一等级", "即将升级")):
        return None
    if candidate in {
        "种子评论",
        "经验值",
        "用户详情",
        "查看详情",
        "发布种子",
        "分享率",
        "上传量",
        "下载量",
        "做种积分",
        "魔力值",
        "未读消息",
    }:
        return None
    return candidate


def _profile_named_level(parser: _NexusHtmlParser, *, site_id: str | None = None) -> str | None:
    site_key = (site_id or "").replace("_", "").lower()
    if site_key == "audiences":
        # PT-depiler: div.ud-hero span.ud-class > b. Audiences moved the
        # current class into its profile hero instead of a NexusPHP table row.
        for value in parser.audiences_level_classes:
            candidate = _normalize_level_candidate(value)
            if candidate is not None:
                return candidate
        for value in parser.class_text_parts.get("ud-class", ()):
            candidate = _normalize_level_candidate(value)
            if candidate is not None:
                return candidate

    # HHClub encodes the class in the CSS class of the current user's profile link.
    # Prefer that explicit marker over neighboring labels such as "经验值" or "种子评论".
    for href, class_value, _label in parser.all_link_classes:
        if urlsplit(href).path.lstrip("/") != "userdetails.php":
            continue
        for class_name in (class_value or "").split():
            if class_name.endswith("_Name") and len(class_name) > len("_Name"):
                candidate = _normalize_level_candidate(
                    class_name[: -len("_Name")].replace("_", " ")
                )
                if candidate is not None:
                    return candidate

    labels = {
        "等级",
        "等級",
        "用户等级",
        "用戶等級",
        "会员等级",
        "會員等級",
        "class",
        "user class",
        "level",
        "user level",
        "level name",
    }
    attribute_priority = {
        ("b", "title"): 0,
        ("strong", "title"): 0,
        ("img", "alt"): 1,
        ("img", "title"): 1,
        ("span", "title"): 2,
        ("span", "aria-label"): 2,
        ("a", "title"): 3,
        ("a", "aria-label"): 3,
    }
    for row in parser.rows:
        for index, cell in enumerate(row.cells[:-1]):
            if _clean_text(cell).rstrip(":：").casefold() not in labels:
                continue
            attributes = (
                row.cell_attributes[index + 1] if index + 1 < len(row.cell_attributes) else []
            )
            for tag, attribute, value in sorted(
                attributes,
                key=lambda item: attribute_priority.get((item[0], item[1]), 10),
            ):
                if (tag, attribute) not in attribute_priority:
                    continue
                candidate = _normalize_level_candidate(value)
                if candidate is not None:
                    return candidate
            parts = row.cell_text_parts[index + 1]
            candidate = _normalize_level_candidate(" ".join(parts))
            if candidate is not None:
                return candidate
    # Do not fall back to the next arbitrary text node on the page: a grade may be
    # represented by an icon with title/alt, and the next text can be a sidebar link.
    return None


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


def _parse_u2_ajax_seeding_summary(html: str) -> tuple[int, int] | None:
    # U2 uses the generic endpoint but a site-specific "N records / size" summary.
    count_match = re.search(
        r"<b\b[^>]*>\s*([0-9][0-9,]*)\s*</b>\s*(?:条记录|條記錄|records?)",
        html,
        re.IGNORECASE,
    )
    visible_text = _clean_text(re.sub(r"<[^>]*>", " ", unescape(html)))
    size_match = re.search(
        r"大小.*?([0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTPE]?i?B)",
        visible_text,
        re.IGNORECASE,
    )
    if count_match is None or size_match is None:
        return None
    count = _parse_nonnegative_int(count_match.group(1))
    size = _parse_size(size_match.group(1))
    return (count, size) if count is not None and size is not None else None


def _parse_pttime_seeding_summary(html: str) -> tuple[int, int] | None:
    parser = _PTTimeSummaryParser()
    parser.feed(html)
    if len(parser.spans) < 4:
        return None
    count_text = _clean_text(" ".join(parser.spans[2]))
    size_text = _clean_text(" ".join(parser.spans[3]))
    count_match = re.search(r"[0-9][0-9,]*", count_text)
    size_match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTPE]?i?B", size_text, re.IGNORECASE)
    if count_match is None or size_match is None:
        return None
    count = _parse_nonnegative_int(count_match.group(0))
    size = _parse_size(size_match.group(0))
    return (count, size) if count is not None and size is not None else None


def _parse_audiences_seeding_summary(parser: _NexusHtmlParser) -> tuple[int, int] | None:
    table_indexes = [row.table_index for row in parser.rows if row.table_index > 0]
    if not table_indexes:
        return None
    first_table = min(table_indexes)
    for row in parser.rows:
        if row.table_index != first_table or len(row.cells) < 3:
            continue
        if not any("total" in _clean_text(cell).casefold() for cell in row.cells):
            continue
        count_match = re.search(r"[0-9][0-9,]*", row.cells[1])
        size_match = re.search(
            r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTPE]?i?B", row.cells[2], re.IGNORECASE
        )
        if count_match is None or size_match is None:
            return None
        count = _parse_nonnegative_int(count_match.group(0))
        size = _parse_size(size_match.group(0))
        return (count, size) if count is not None and size is not None else None
    return None


def _parse_pter_uploaded_total(html: str) -> int | None:
    pager = re.search(
        r"""<p\b[^>]*class\s*=\s*["'][^"']*\bnp-pager\b[^"']*["'][^>]*>(.*?)</p>""",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if pager is None:
        # PT-depiler treats a valid search page without a pager as an empty result.
        return 0
    bold_values = re.findall(r"<b\b[^>]*>(.*?)</b>", pager.group(1), re.IGNORECASE | re.DOTALL)
    if not bold_values:
        return 0
    last_bold = _clean_text(re.sub(r"<[^>]*>", " ", unescape(bold_values[-1])))
    match = re.search(r"([0-9][0-9,]*)\s*$", last_bold)
    return _parse_nonnegative_int(match.group(1)) if match is not None else 0


def _parse_pter_seeding_count(parser: _NexusHtmlParser) -> int | None:
    for href, title, label in parser.all_links:
        parsed = urlsplit(href)
        if _script_name(parsed.path) != "getusertorrentlist.php":
            continue
        if parse_qs(parsed.query).get("type") != ["seeding"]:
            continue
        match = re.search(r"[0-9][0-9,]*", _clean_text(f"{label} {title or ''}"))
        if match is not None:
            return _parse_nonnegative_int(match.group(0))
    return None


def _parse_u2_bonus_per_hour(html: str) -> float | None:
    # PT-depiler reads the daily UCoin rate from /mprecent.php and divides by 24.
    match = re.search(r"UCoin\s*([0-9][0-9,]*(?:\.[0-9]+)?)", html, re.IGNORECASE)
    if match is None:
        match = re.search(r"UCoin([0-9][0-9,]*(?:\.[0-9]+)?)", html, re.IGNORECASE)
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", "")) / 24
    except ValueError:
        return None
    return value if 0 <= value < float("inf") else None


def _parse_hhclub_profile_bonus_per_hour(parser: _NexusHtmlParser) -> float | None:
    for text in parser.class_text_parts.get("row-span-4", ()):
        match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", text)
        if match is None:
            continue
        try:
            value = float(match.group(0).replace(",", ""))
        except ValueError:
            continue
        if 0 <= value < float("inf"):
            return value
    return None


def _json_nonnegative_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _json_nonnegative_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        return parsed if 0 <= parsed < float("inf") else None
    if isinstance(value, str):
        try:
            parsed = float(value.replace(",", "").strip())
        except ValueError:
            return None
        return parsed if 0 <= parsed < float("inf") else None
    return None


def _parse_keepfrds_api_profile(
    profile: SiteUserProfile, payload_text: str, *, expected_uid: str | None
) -> SiteUserProfile:
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        raise SiteAdapterError(
            "SITE_INVALID_RESPONSE", "KeepFrds 用户统计 API 返回的 JSON 无效"
        ) from exc
    if not isinstance(payload, dict):
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "KeepFrds 用户统计 API 结构无效")
    user = payload.get("user")
    stats = payload.get("torrentStats")
    if not isinstance(user, dict) or not isinstance(stats, dict):
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "KeepFrds 用户统计 API 缺少必要对象")

    raw_uid = user.get("id")
    api_uid = str(raw_uid) if _json_nonnegative_int(raw_uid) is not None else None
    if expected_uid is not None and api_uid is not None and api_uid != expected_uid:
        raise SiteAdapterError("SITE_INVALID_RESPONSE", "KeepFrds API 用户 ID 与当前登录用户不一致")
    raw_class = user.get("class")
    class_id = _json_nonnegative_int(raw_class)
    api_level = _KEEPFRDS_CLASS_NAMES.get(class_id) if class_id is not None else None
    if api_level is None and isinstance(raw_class, str) and not raw_class.isdecimal():
        api_level = _normalize_level_candidate(raw_class)

    uploaded = _json_nonnegative_int(user.get("uploadedBytes"))
    downloaded = _json_nonnegative_int(user.get("downloadedBytes"))
    ratio = profile.ratio
    if uploaded is not None and downloaded is not None:
        ratio = uploaded / downloaded if downloaded > 0 else None

    joined_at = user.get("joinedAt")
    join_time = profile.join_time
    if isinstance(joined_at, str) and joined_at.strip():
        try:
            parsed_join_time = datetime.fromisoformat(joined_at.strip().replace("Z", "+00:00"))
            if parsed_join_time.tzinfo is None or parsed_join_time.utcoffset() is None:
                parsed_join_time = parsed_join_time.replace(tzinfo=UTC)
            join_time = parsed_join_time
        except ValueError:
            pass

    raw_username = user.get("username")
    api_username = (
        raw_username.strip() if isinstance(raw_username, str) and raw_username.strip() else None
    )
    return replace(
        profile,
        uid=api_uid or profile.uid,
        username=api_username or profile.username,
        user_level=api_level or profile.user_level,
        uploaded_bytes=uploaded if uploaded is not None else profile.uploaded_bytes,
        downloaded_bytes=downloaded if downloaded is not None else profile.downloaded_bytes,
        ratio=ratio,
        torrents_posted=(
            _json_nonnegative_int(stats.get("uploaded"))
            if _json_nonnegative_int(stats.get("uploaded")) is not None
            else profile.torrents_posted
        ),
        seeding_count=(
            _json_nonnegative_int(stats.get("seeding"))
            if _json_nonnegative_int(stats.get("seeding")) is not None
            else profile.seeding_count
        ),
        seeding_size_bytes=(
            _json_nonnegative_int(stats.get("seedingBytes"))
            if _json_nonnegative_int(stats.get("seedingBytes")) is not None
            else profile.seeding_size_bytes
        ),
        bonus=(
            _json_nonnegative_float(user.get("bonus"))
            if _json_nonnegative_float(user.get("bonus")) is not None
            else profile.bonus
        ),
        join_time=join_time,
    )


def _parse_springsunday_ajax_summary(html: str) -> tuple[int, int] | None:
    """SpringSunday exposes full seeding totals as the first two bold values."""
    marker = re.search(
        r"<b\b[^>]*>\s*([0-9][0-9,]*)\s*</b>\s*(?:条记录|條記錄|records?)",
        html,
        re.IGNORECASE,
    )
    if marker is None:
        return None
    bold_values = re.findall(r"<b\b[^>]*>(.*?)</b\s*>", html, re.IGNORECASE | re.DOTALL)
    if len(bold_values) < 2:
        return None
    count_text = _clean_text(re.sub(r"<[^>]*>", " ", unescape(bold_values[0])))
    size_text = _clean_text(re.sub(r"<[^>]*>", " ", unescape(bold_values[1])))
    count = _parse_nonnegative_int(count_text)
    size = _parse_size(size_text)
    return (count, size) if count is not None and size is not None else None


def _parse_nexus_ajax_total_count(html: str, parser: _NexusHtmlParser) -> int | None:
    """Read NexusPHP upload totals using the response variants handled by PT-Depiler."""
    match = re.search(
        r"<b\b[^>]*>\s*([0-9][0-9,]*)\s*</b>\s*(?:条记录|條記錄|records?)",
        html,
        re.IGNORECASE,
    )
    if match is not None:
        return _parse_nonnegative_int(match.group(1))

    # Several NexusPHP sites return only this short marker when the user has no
    # published torrents (Audiences currently returns exactly "没有记录").
    if not parser.rows and re.search(r"(?:No records?\.?|没有记录|沒有記錄)", html, re.IGNORECASE):
        return 0

    # PT-Depiler also recognizes the aggregate "count | size" marker before the
    # table. Prefer it over row counts so pagination does not undercount uploads.
    pre_table = re.split(r"<table\b", html, maxsplit=1, flags=re.IGNORECASE)[0]
    pre_table_text = " ".join(_parse_html(pre_table).text_parts)
    pipe_summary = re.search(
        r"(?<![0-9,])([0-9][0-9,]*)\s*\|\s*([0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B)\b",
        pre_table_text,
        re.IGNORECASE,
    )
    if pipe_summary is not None:
        return _parse_nonnegative_int(pipe_summary.group(1))

    table_index = max((row.table_index for row in parser.rows), default=0)
    rows = [row for row in parser.rows if row.table_index == table_index]
    return max(0, len(rows) - 1) if rows else None


def _parse_nexus_ajax_seeding_summary(
    html: str, parser: _NexusHtmlParser, *, spring_sunday: bool = False
) -> tuple[int, int] | None:
    if spring_sunday:
        summary = _parse_springsunday_ajax_summary(html)
        if summary is not None:
            return summary
    explicit = _parse_explicit_seeding_summary(parser)
    if explicit is not None:
        return explicit
    pre_table = re.split(r"<table\b", html, maxsplit=1, flags=re.IGNORECASE)[0]
    pre_table_text = " ".join(_parse_html(pre_table).text_parts)
    pipe_summary = re.search(
        r"(?<![0-9,])([0-9][0-9,]*)\s*\|\s*([0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B)\b",
        pre_table_text,
        re.IGNORECASE,
    )
    if pipe_summary is not None:
        count = _parse_nonnegative_int(pipe_summary.group(1))
        size = _parse_size(pipe_summary.group(2))
        if count is not None and size is not None:
            return count, size
    table_index = max((row.table_index for row in parser.rows), default=0)
    rows = [row for row in parser.rows if row.table_index == table_index]
    data_rows = rows[1:] if len(rows) > 1 else []
    if not data_rows:
        return None
    size_index = next(
        (index for index, cell in enumerate(data_rows[0].cells) if _parse_size(cell) is not None),
        None,
    )
    if size_index is None:
        return None
    total_size = 0
    for row in data_rows:
        if size_index >= len(row.cells):
            return None
        size = _parse_size(row.cells[size_index])
        if size is None:
            return None
        total_size += size
    return len(data_rows), total_size


def _parse_springsunday_bonus_per_hour(parser: _NexusHtmlParser) -> float | None:
    row = next((item for item in parser.rows if "nowrap" in item.classes and item.cells), None)
    if row is None:
        return None
    match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", _clean_text(row.cells[-1]))
    if match is None:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if 0 <= value < float("inf") else None


def _parse_nexus_bonus_per_hour(
    parser: _NexusHtmlParser, *, class_name: str | None = None
) -> float | None:
    """Read the current hourly rate using PT-Depiler's selectors and sentence variants."""
    class_value = _profile_class_float(parser, class_name) if class_name else None
    if class_value is not None:
        return class_value

    text = "".join(parser.text_parts)
    match = re.search(
        r"(?:你当前每小时能获取|你當前每小時能獲取|You are currently getting)"
        r"\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE,
    )
    if match is not None:
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
        return value if 0 <= value < float("inf") else None

    return _profile_float_from_parser(
        parser,
        text,
        "每小时魔力值",
        "每小时魔力",
        "每小时憨豆",
        "Bonus per hour",
    )


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


def _profile_value_from_parser(
    parser: _NexusHtmlParser,
    text: str,
    labels: tuple[str, ...],
    value_pattern: str,
) -> str | None:
    normalized_labels = {_clean_text(label).rstrip(":：").casefold() for label in labels}
    exact_label_seen = False
    for row in parser.rows:
        for index, cell in enumerate(row.cells):
            if _clean_text(cell).rstrip(":：").casefold() not in normalized_labels:
                continue
            exact_label_seen = True
            if index + 1 >= len(row.cells):
                continue
            value_cell = row.cells[index + 1]
            match = re.search(rf"({value_pattern})", value_cell, re.IGNORECASE)
            if match is not None:
                return _clean_text(match.group(1))
            if index + 1 < len(row.cell_attributes):
                for _tag, _attribute, attribute_value in row.cell_attributes[index + 1]:
                    match = re.search(rf"({value_pattern})", attribute_value, re.IGNORECASE)
                    if match is not None:
                        return _clean_text(match.group(1))
    # Some derivatives put several labelled values into a single profile cell.
    for row in parser.rows:
        for cell in row.cells:
            for label in labels:
                match = re.search(
                    rf"{re.escape(label)}\s*[:：]?\s*({value_pattern})(?=\s|$)",
                    cell,
                    re.IGNORECASE,
                )
                if match is not None:
                    return _clean_text(match.group(1))
    # If a real table row declared the field but had no value, do not accidentally
    # consume a numeric value from the next row (e.g. ratio=∞ followed by points).
    if exact_label_seen:
        return None
    return _profile_value(text, labels, value_pattern)


def _profile_class_text(parser: _NexusHtmlParser, class_name: str) -> str | None:
    for value in parser.class_text_parts.get(class_name, ()):
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _profile_class_size(parser: _NexusHtmlParser, class_name: str) -> int | None:
    value = _profile_class_text(parser, class_name)
    if value is None:
        return None
    match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTPE]?i?B", value, re.IGNORECASE)
    return _parse_size(match.group(0)) if match is not None else None


def _profile_class_float(parser: _NexusHtmlParser, class_name: str) -> float | None:
    value = _profile_class_text(parser, class_name)
    if value is None:
        return None
    match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", value)
    if match is None:
        return None
    try:
        parsed = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return parsed if 0 <= parsed < float("inf") else None


def _profile_class_int(parser: _NexusHtmlParser, class_name: str) -> int | None:
    value = _profile_class_text(parser, class_name)
    if value is None:
        return None
    match = re.search(r"[0-9][0-9,]*", value)
    return _parse_nonnegative_int(match.group(0)) if match is not None else None


def _profile_size_from_parser(parser: _NexusHtmlParser, text: str, *labels: str) -> int | None:
    value = _profile_value_from_parser(
        parser, text, labels, r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTPE]?i?B"
    )
    return _parse_size(value or "")


def _profile_int_from_parser(parser: _NexusHtmlParser, text: str, *labels: str) -> int | None:
    value = _profile_value_from_parser(parser, text, labels, r"[0-9][0-9,]*")
    return _parse_nonnegative_int(value or "")


def _profile_float_from_parser(parser: _NexusHtmlParser, text: str, *labels: str) -> float | None:
    value = _profile_value_from_parser(parser, text, labels, r"[0-9][0-9,]*(?:\.[0-9]+)?")
    if value is None:
        return None
    try:
        parsed = float(value.replace(",", ""))
    except ValueError:
        return None
    return parsed if 0 <= parsed < float("inf") else None


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


def _profile_datetime(text: str, *labels: str, timezone_offset_minutes: int) -> datetime | None:
    value = _profile_text(text, *labels, max_length=256)
    if value is None:
        return None
    normalized = value.split(" (", 1)[0].strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=timezone_offset_minutes)))
        return parsed
    return None


def _profile_size(text: str, *labels: str) -> int | None:
    value = _profile_value(text, labels, r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTPE]?i?B")
    return _parse_size(value or "")


def _profile_int(text: str, *labels: str) -> int | None:
    value = _profile_value(text, labels, r"[0-9][0-9,]*")
    return _parse_nonnegative_int(value or "")


def _profile_float(text: str, *labels: str) -> float | None:
    value = _profile_value(text, labels, r"[0-9][0-9,]*(?:\.[0-9]+)?")
    if value is None:
        return None
    try:
        parsed = float(value.replace(",", ""))
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
    match = _SIZE_RE.fullmatch(_clean_text(value).replace(",", ""))
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


def _detail_external_ids(
    links: list[tuple[str, str | None, str]], display_name: str
) -> tuple[ExternalMediaId, ...]:
    """Keep only unambiguous page IDs when a torrent represents multiple films.

    Detail pages can include several IMDb links in a collection synopsis. The
    generic media descriptor intentionally permits only one ID per namespace,
    so prefer IDs parsed from the torrent title and only import a page-link ID
    when that namespace has exactly one distinct value.
    """
    title_namespaces = {item.namespace for item in parse_media_name(display_name).external_ids}
    by_namespace: dict[str, set[str]] = {}
    for item in _external_ids(links):
        if item.namespace in title_namespaces:
            continue
        by_namespace.setdefault(item.namespace, set()).add(item.value)
    return tuple(
        ExternalMediaId(namespace, next(iter(values)))
        for namespace, values in sorted(by_namespace.items())
        if len(values) == 1
    )


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
    if not meaningful:
        return None
    candidate = meaningful[-1]
    # Some NexusPHP derivatives expose only `种子详情 "title"` or
    # `Torrent Details "title"` in <title>, without the usual `site ::` prefix.
    # Recognize that explicit detail-title form before rejecting single-part
    # titles; continue rejecting generic one-part page titles such as the site name.
    match = _DETAIL_TITLE_RE.fullmatch(candidate)
    if match is not None:
        return _clean_text(match.group(1))
    if len(meaningful) < 2:
        return None
    return candidate


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
