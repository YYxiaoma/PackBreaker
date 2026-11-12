from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx2
import pytest

from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_search import SearchMediaType, SearchQuery, SearchSortHint
from backend.app.infrastructure.adapters.nexusphp import (
    HDTimeAdapter,
    HHClubAdapter,
    NexusPhpProfile,
    NexusPhpWebAdapter,
    _parse_html,
    _parse_nexus_ajax_seeding_summary,
    _parse_nexus_ajax_total_count,
    _profile_named_level,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from tests.contract.site_adapter_contract import (
    SiteAdapterContractCase,
    assert_read_only_site_adapter_contract,
)

_COOKIE = "uid=synthetic; pass=synthetic-secret"
_TORRENT = b"d4:infod4:name9:syntheticee"


def _hhclub_transport(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> httpx2.MockTransport:
    """Keep existing HHClub tests focused on their feature while allowing its seeding probe."""

    def dispatch(request: httpx2.Request) -> httpx2.Response:
        if (
            request.url.host == "hhanclub.net"
            and request.url.path == "/getusertorrentlistajax.php"
            and request.url.params.get("type") == "seeding"
        ):
            return httpx2.Response(200, text="<div>synthetic seeding fragment without totals</div>")
        return handler(request)

    return httpx2.MockTransport(dispatch)


@pytest.mark.asyncio
async def test_springsunday_profile_uses_site_specific_bonus_and_ajax_totals() -> None:
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.url.path, dict(request.url.params)))
        assert request.url.host == "springsunday.net"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<div id="info_block"><a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99"></a></div>'
                    '<a href="userdetails.php?id=100">OtherUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<div id="info_block"><a href="userdetails.php?id=99">YYxiaoma</a></div>'
                    '<a href="messages.php"><b style="background: darkorange">8</b></a>'
                    "<table>"
                    '<tr><td class="rowhead">Join date</td><td>2021-02-03 04:05:06</td></tr>'
                    '<tr><td class="rowhead">Last Action</td>'
                    "<td>2026-11-04 10:11:12 (today)</td></tr>"
                    '<tr><td class="rowhead">传输</td>'
                    "<td>上传量 <b>644.07 GiB</b> 下载量 <b>0 B</b> 分享率 <b>∞</b></td></tr>"
                    '<tr><td class="rowhead">积分</td><td>10,577.70</td></tr>'
                    '<tr><td class="rowhead">做种积分</td><td>10,544.90</td></tr>'
                    "</table>"
                ),
            )
        if request.url.path == "/mybonus.php":
            return httpx2.Response(
                200,
                text=(
                    '<table><tbody><tr class="nowrap"><td>1</td><td>2</td><td>3</td>'
                    "<td>4</td><td>5</td><td>6</td><td>7</td><td>8</td><td>12.345</td>"
                    "</tr></tbody></table>"
                ),
            )
        if request.url.path == "/getusertorrentlistajax.php":
            if request.url.params.get("type") == "seeding":
                return httpx2.Response(
                    200,
                    text=(
                        "<div><b>48</b>条记录 / 总大小 <b>1.09 TiB</b></div>"
                        "<table><tr><td>标题</td></tr><tr><td>样本</td></tr></table>"
                    ),
                )
            if request.url.params.get("type") == "uploaded":
                return httpx2.Response(
                    200,
                    text="<div><b>0</b>条记录</div><table><tr><td>标题</td></tr></table>",
                )
        raise AssertionError(f"Unexpected request: {request.url}")

    adapter = NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="spring_sunday",
            default_base_url="https://springsunday.net",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    )
    profile = await adapter.fetch_user_profile()
    assert profile.username == "YYxiaoma"
    assert profile.join_time is not None
    assert profile.join_time.strftime("%Y-%m-%dT%H:%M:%S") == "2021-02-03T04:05:06"
    assert profile.last_access_at is not None
    assert profile.last_access_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-11-04T10:11:12"
    assert profile.message_count == 8
    assert profile.uploaded_bytes == int(644.07 * 1024**3)
    assert profile.downloaded_bytes == 0
    assert profile.torrents_posted == 0
    assert profile.seeding_count == 48
    assert profile.seeding_size_bytes == int(1.09 * 1024**4)
    assert profile.bonus == 10577.70
    assert profile.seeding_points == 10544.90
    assert profile.bonus_per_hour == 12.345
    assert [path for path, _params in requests] == [
        "/index.php",
        "/userdetails.php",
        "/mybonus.php",
        "/getusertorrentlistajax.php",
        "/getusertorrentlistajax.php",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("date_header", (False, True))
async def test_nexusphp_429_preserves_bounded_retry_after(date_header: bool) -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    header = (
        format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
        if date_header
        else "120"
    )
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.url.host == "hdtime.org"
        assert request.headers.get("cookie") == _COOKIE
        assert request.url.path == "/torrents.php"
        return httpx2.Response(429, headers={"Retry-After": header})

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.search(SearchQuery(("Synthetic",), SearchMediaType.MOVIE))
    assert failure.value.code == "SITE_RATE_LIMITED"
    assert failure.value.retryable is True
    assert failure.value.retry_after_seconds is not None
    assert 115 <= failure.value.retry_after_seconds <= 121
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_hdtime_details_tolerates_a_small_number_of_invalid_utf8_bytes() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hdtime.org"
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            content=(
                b"<html><body><h1>Synthetic.Movie.2026</h1><!-- malformed: \xff -->"
                b'<a href="usercp.php">profile</a>'
                b'<a href="download.php?id=123">download</a></body></html>'
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )

    assert details.candidate.display_name == "Synthetic.Movie.2026"


@pytest.mark.asyncio
async def test_hdtime_details_ignores_ambiguous_external_ids_from_collection_page() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                "<html><body><h1>Synthetic.Collection.1987-1991</h1>"
                '<a href="usercp.php">profile</a>'
                '<a href="https://www.imdb.com/title/tt0093978/">IMDb 1987</a>'
                '<a href="https://www.imdb.com/title/tt0100625/">IMDb 1990</a>'
                '<a href="https://www.imdb.com/title/tt0102932/">IMDb 1991</a>'
                '<a href="https://movie.douban.com/subject/1297447/">Douban</a>'
                '<a href="download.php?id=123">download</a></body></html>'
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )

    assert {(item.namespace, item.value) for item in details.candidate.descriptor.external_ids} == {
        ("douban", "1297447")
    }


@pytest.mark.asyncio
async def test_hdtime_details_prefers_title_external_id_over_conflicting_page_link() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                "<html><body><h1>Synthetic.Movie.2026.tt1234567</h1>"
                '<a href="usercp.php">profile</a>'
                '<a href="https://www.imdb.com/title/tt7654321/">Related IMDb</a>'
                '<a href="https://movie.douban.com/subject/1297447/">Douban</a>'
                '<a href="download.php?id=123">download</a></body></html>'
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )

    assert {(item.namespace, item.value) for item in details.candidate.descriptor.external_ids} == {
        ("douban", "1297447"),
        ("imdb", "tt1234567"),
    }


@pytest.mark.asyncio
async def test_hdtime_details_preserves_unique_external_ids_from_page_links() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                "<html><body><h1>Synthetic.Movie.2026</h1>"
                '<a href="usercp.php">profile</a>'
                '<a href="https://www.imdb.com/title/tt1234567/">IMDb</a>'
                '<a href="https://movie.douban.com/subject/1297447/">Douban</a>'
                '<a href="download.php?id=123">download</a></body></html>'
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )

    assert {(item.namespace, item.value) for item in details.candidate.descriptor.external_ids} == {
        ("douban", "1297447"),
        ("imdb", "tt1234567"),
    }


@pytest.mark.asyncio
async def test_hdtime_details_treats_site_frequency_page_as_retryable_rate_limit() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text="<html><head><title>HDTIME</title></head><body><h2>访问过于频繁</h2></body></html>",
        )

    with pytest.raises(SiteAdapterError) as failure:
        await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details("123")

    assert failure.value.code == "SITE_RATE_LIMITED"
    assert failure.value.retryable is True
    assert failure.value.retry_after_seconds == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail_rows", "expected_size"),
    [
        ("<tr><td>大小：</td><td>7.25 GiB</td></tr>", int(7.25 * 1024**3)),
        ("<tr><td>Size</td><td>4.00 GiB</td></tr>", 4 * 1024**3),
        ("<tr><td>用户总大小</td><td>8.00 GiB</td></tr>", None),
        (
            "<tr><td>大小</td><td>4.00 GiB</td></tr><tr><td>大小</td><td>8.00 GiB</td></tr>",
            None,
        ),
        ("<tr><td>大小</td><td>unknown</td></tr>", None),
    ],
)
async def test_nexusphp_details_only_accept_explicit_unambiguous_size(
    detail_rows: str, expected_size: int | None
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hdtime.org"
        assert request.url.path == "/details.php"
        assert request.url.params["id"] == "123"
        return httpx2.Response(
            200,
            text=(
                "<html><body><h1>Synthetic.Movie.2026</h1>"
                f"<table>{detail_rows}</table>"
                '<a href="download.php?id=123">Download</a>'
                "</body></html>"
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )
    assert details.site_id == "hdtime"
    assert details.torrent_id == "123"
    assert details.candidate.total_size == expected_size


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_title", "expected_title"),
    [
        ('种子详情 "Synthetic.Movie.2026"', "Synthetic.Movie.2026"),
        ('Torrent Details "Synthetic.Movie.2026"', "Synthetic.Movie.2026"),
    ],
)
async def test_nexusphp_details_accepts_unbranded_explicit_detail_title(
    page_title: str, expected_title: str
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hdtime.org"
        assert request.url.path == "/details.php"
        assert request.url.params["id"] == "123"
        return httpx2.Response(
            200,
            text=(
                f"<html><head><title>{page_title}</title></head><body>"
                '<a href="usercp.php">profile</a>'
                '<a href="download.php?id=123">download</a>'
                "</body></html>"
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )
    assert details.candidate.display_name == expected_title


@pytest.mark.asyncio
async def test_nexusphp_details_rejects_generic_single_part_page_title() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                "<html><head><title>HDTime</title></head><body>"
                '<a href="usercp.php">profile</a>'
                '<a href="download.php?id=123">download</a>'
                "</body></html>"
            ),
        )

    with pytest.raises(SiteAdapterError) as failure:
        await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details("123")
    assert failure.value.code == "SITE_INVALID_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head_markup", "body_markup"),
    [
        ('<meta property="og:title" content="Synthetic.Movie.2026">', ""),
        ("", "<h2>Synthetic.Movie.2026</h2>"),
        ("", '<div class="torrentname">Synthetic.Movie.2026</div>'),
    ],
)
async def test_nexusphp_details_uses_alternate_title_markup_with_matching_download_link(
    head_markup: str, body_markup: str
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                f"<html><head><title>HDTime</title>{head_markup}</head><body>"
                f"{body_markup}"
                '<a href="usercp.php">profile</a>'
                '<a href="download.php?id=123">download</a>'
                "</body></html>"
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )
    assert details.candidate.display_name == "Synthetic.Movie.2026"


@pytest.mark.asyncio
async def test_hdtime_adapter_satisfies_shared_contract_with_cookie_only_on_origin() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.host == "hdtime.org"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(200, text='<a href="usercp.php">profile</a>')
        if request.url.path == "/torrents.php":
            return httpx2.Response(200, text=_search_html())
        if request.url.path == "/details.php":
            return httpx2.Response(200, text=_details_html())
        if request.url.path == "/download.php":
            return httpx2.Response(200, content=_TORRENT)
        return httpx2.Response(404)

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    await assert_read_only_site_adapter_contract(
        SiteAdapterContractCase(
            adapter,
            SearchQuery(("Synthetic", "Movie", "2026"), SearchMediaType.MOVIE),
            "hdtime",
            "123",
        )
    )
    page = await adapter.search(SearchQuery(("Synthetic", "Movie", "2026"), SearchMediaType.MOVIE))
    candidate = page.items[0]
    assert candidate.total_size == 4 * 1024**3
    assert candidate.category == "401"
    assert candidate.seeders == 8
    assert candidate.leechers == 2
    assert candidate.published_at is not None
    assert candidate.published_at.isoformat() == "2026-09-09T04:00:00+00:00"
    assert {(item.namespace, item.value) for item in candidate.descriptor.external_ids} == {
        ("douban", "1295644"),
        ("imdb", "tt1234567"),
    }

    search = next(request for request in requests if request.url.path == "/torrents.php")
    params = parse_qs(search.url.query.decode())
    assert params["search"] == ["synthetic movie 2026"]
    assert params["page"] == ["0"]
    assert params["incldead"] == ["1"]
    assert all("synthetic-secret" not in str(request.url) for request in requests)


@pytest.mark.asyncio
async def test_hdtime_search_recovers_size_when_trailing_columns_shift_the_configured_offset() -> (
    None
):
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/torrents.php"
        html = _search_html().replace(
            "<td>10</td><td>status</td>",
            "<td>10</td><td>status</td><td>extra trailing column</td>",
        )
        return httpx2.Response(200, text=html)

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    page = await adapter.search(SearchQuery(("Synthetic",), SearchMediaType.MOVIE))

    assert len(page.items) == 1
    assert page.items[0].total_size == 4 * 1024**3


@pytest.mark.asyncio
async def test_hdtime_details_reads_size_from_dedicated_torrent_size_class() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                "<html><body><h1>Synthetic.Movie.2026</h1>"
                '<a href="usercp.php">profile</a>'
                '<div class="torrent-info-text-size">4.00 GiB</div>'
                '<a href="download.php?id=123">download</a></body></html>'
            ),
        )

    details = await HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler)).fetch_details(
        "123"
    )

    assert details.candidate.total_size == 4 * 1024**3


@pytest.mark.asyncio
async def test_hdtime_search_uses_external_id_and_stable_sort_mapping() -> None:
    seen_query: list[dict[str, list[str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_query.append(parse_qs(request.url.query.decode()))
        return httpx2.Response(200, text=_search_html(empty=True))

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    await adapter.search(
        SearchQuery(
            ("ignored",),
            SearchMediaType.MOVIE,
            external_ids=(ExternalMediaId("imdb", "tt1234567"),),
            page=3,
            sort=SearchSortHint.SEEDERS,
        )
    )

    assert seen_query[0]["search"] == ["tt1234567"]
    assert seen_query[0]["search_area"] == ["4"]
    assert seen_query[0]["page"] == ["2"]
    assert seen_query[0]["sort"] == ["7"]


@pytest.mark.asyncio
async def test_hdtime_advertises_only_supported_external_id_search_modes() -> None:
    capabilities = await HDTimeAdapter(_COOKIE).capabilities()

    assert capabilities.supports_imdb_id is True
    assert capabilities.supports_douban_id is False


@pytest.mark.asyncio
async def test_hdtime_user_profile_is_loaded_on_demand_from_same_origin() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_paths.append(request.url.path)
        assert request.url.host == "hdtime.org"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99">SyntheticUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            assert request.url.params["id"] == "99"
            return httpx2.Response(
                200,
                text="""
                <html><body>
                  <a href="usercp.php">profile</a>
                  <table>
                    <tr><td>用户等级</td><td>Elite</td></tr>
                    <tr><td>真实上传量</td><td>2.00 TiB</td></tr>
                    <tr><td>上传量</td><td>3.00 TiB</td></tr>
                    <tr><td>下载量</td><td>1.50 TiB</td></tr>
                    <tr><td>发种数</td><td>12</td></tr>
                    <tr><td>做种数</td><td>34</td></tr>
                    <tr><td>做种量</td><td>4.00 TiB</td></tr>
                    <tr><td>魔力值</td><td>56.75</td></tr>
                    <tr><td>每小时魔力值</td><td>7.25</td></tr>
                    <tr><td>加入日期</td><td>2024-01-15 10:30:22</td></tr>
                    <tr><td>最近动向</td><td>2026-10-16 10:20:00</td></tr>
                    <tr><td>H&R 状态</td><td>正常</td></tr>
                    <tr><td>H&R 次数</td><td>0</td></tr>
                    <tr><td>未读消息</td><td>3</td></tr>
                    <tr><td colspan="2"><a href="myhr.php?userid=42">H&amp;R: 2/0/5</a></td></tr>
                  </table>
                </body></html>
                """,
            )
        return httpx2.Response(404)

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()

    assert requested_paths == ["/index.php", "/userdetails.php"]
    assert profile.site_id == "hdtime"
    assert profile.uid == "99"
    assert profile.username == "SyntheticUser"
    assert profile.user_level == "Elite"
    assert profile.real_uploaded_bytes == 2 * 1024**4
    assert profile.real_downloaded_bytes is None
    assert profile.uploaded_bytes == 3 * 1024**4
    assert profile.downloaded_bytes == int(1.5 * 1024**4)
    assert profile.ratio == 2.0
    assert profile.torrents_posted == 12
    assert profile.seeding_count == 34
    assert profile.seeding_size_bytes == 4 * 1024**4
    assert profile.bonus == 56.75
    assert profile.seeding_points is None
    assert profile.bonus_per_hour == 7.25
    assert profile.join_time is not None
    assert profile.join_time.isoformat() == "2024-01-15T02:30:22+00:00"
    assert profile.last_access_at is not None
    assert profile.last_access_at.isoformat() == "2026-10-16T02:20:00+00:00"
    assert profile.hnr_status == "正常"
    assert profile.hnr_count == 0
    assert profile.message_count == 3


@pytest.mark.asyncio
async def test_hdtime_profile_numeric_grade_is_not_shown_as_name_and_supports_stat_labels() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hdtime.org"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        assert request.url.path == "/userdetails.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">profile</a><table>'
                "<tr><td>用户等级</td><td>7</td></tr>"
                "<tr><td>发布数</td><td>5</td></tr>"
                "<tr><td>正在做种</td><td>24</td></tr>"
                "<tr><td>当前做种量</td><td>4.00 TiB</td></tr>"
                "<tr><td>做种积分</td><td>12.5</td></tr>"
                "<tr><td>每小时魔力</td><td>1.25</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level is None
    assert profile.torrents_posted == 5
    assert profile.seeding_count == 24
    assert profile.seeding_size_bytes == 4 * 1024**4
    assert profile.seeding_points == 12.5
    assert profile.bonus_per_hour == 1.25


@pytest.mark.asyncio
async def test_hdtime_multinode_grade_keeps_whole_official_name() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hdtime.org"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        assert request.url.path == "/userdetails.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">profile</a><table>'
                "<tr><td>等级</td><td><b>INSANE</b><br><span>USER</span></td></tr>"
                "<tr><td>魔力值</td><td>11</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "INSANE USER"
    assert profile.seeding_count is None


@pytest.mark.asyncio
async def test_hdtime_non_numeric_grade_does_not_consume_next_profile_row() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">profile</a><table>'
                "<tr><td>等级</td><td><strong>Member</strong></td></tr>"
                "<tr><td>做种数</td><td>37</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "Member"
    assert profile.seeding_count == 37


@pytest.mark.asyncio
async def test_hdtime_gets_explicit_count_and_size_from_own_seeding_summary() -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "hdtime.org"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php" and not request.url.params.get("action"):
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        assert request.url.params["action"] == "2"
        assert request.url.params["id"] == "99"
        return httpx2.Response(
            200,
            text='<a href="usercp.php">profile</a><p>37 条记录 | 总大小：4.50 TiB</p>',
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert paths == ["/index.php", "/userdetails.php", "/userdetails.php"]
    assert profile.user_level == "Member"
    assert profile.seeding_count == 37
    assert profile.seeding_size_bytes == int(4.5 * 1024**4)


@pytest.mark.asyncio
async def test_hdtime_uses_own_seeding_ajax_header_when_detail_is_only_a_dynamic_shell() -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert request.url.host == "hdtime.org"
        assert request.headers.get("cookie") == _COOKIE
        requested.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php" and not request.url.params.get("action"):
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        if request.url.path == "/userdetails.php":
            assert dict(request.url.params) == {"id": "99", "action": "2"}
            return httpx2.Response(200, text="<p>当前做种列表动态加载</p>")
        assert request.url.path == "/getusertorrentlistajax.php"
        assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
        return httpx2.Response(
            200,
            text=(
                "<div><b>37</b> 条记录 | 总大小：<strong>4.500 TB</strong></div>"
                '<p class="nexus-pagination">1 - 10 | 11 - 20</p>'
                "<table><tr><td>仅用于合成测试的首条记录</td><td>23 GB</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == [
        ("/index.php", {}),
        ("/userdetails.php", {"id": "99"}),
        ("/userdetails.php", {"id": "99", "action": "2"}),
        ("/getusertorrentlistajax.php", {"userid": "99", "type": "seeding"}),
    ]
    assert profile.user_level == "Member"
    assert profile.seeding_count == 37
    assert profile.seeding_size_bytes == int(4.5 * 1024**4)


@pytest.mark.asyncio
async def test_hdtime_complete_ajax_summary_replaces_stale_partial_profile_pair() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php" and not request.url.params.get("action"):
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                    "<table><tr><td>做种数</td><td>8</td></tr></table>"
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(200, text="<p>动态加载</p>")
        assert request.url.path == "/getusertorrentlistajax.php"
        assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
        return httpx2.Response(200, text="<div><b>37</b> 条记录 | 总大小：4.500 TB</div>")

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == [
        "/index.php",
        "/userdetails.php",
        "/userdetails.php",
        "/getusertorrentlistajax.php",
    ]
    assert profile.seeding_count == 37
    assert profile.seeding_size_bytes == int(4.5 * 1024**4)


@pytest.mark.asyncio
async def test_hdtime_optional_ajax_failure_does_not_discard_main_profile() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php" and not request.url.params.get("action"):
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(200, text="<p>动态加载</p>")
        assert request.url.path == "/getusertorrentlistajax.php"
        return httpx2.Response(403)

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "Member"
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fragment",
    [
        '<p class="nexus-pagination">1 - 100 | 101 - 200</p><table><tr><td>23 GB</td></tr></table>',
        "<p>3 条记录 | 总大小：2 TB</p><p>5 条记录 | 总大小：7 TB</p>",
        "<div><b>37</b> 条记录 | 总大小：--</div>",
        (
            "<div><b>37</b> 条记录 | 总大小：--</div>"
            "<table><tr><td>5 条记录 | 总大小：7 TB</td></tr></table>"
        ),
    ],
)
async def test_hdtime_ajax_does_not_guess_from_pagination_or_invalid_aggregates(
    fragment: str,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php" and not request.url.params.get("action"):
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(200, text="<p>动态加载</p>")
        assert request.url.path == "/getusertorrentlistajax.php"
        return httpx2.Response(200, text=fragment)

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["／", "/", "｜"])
async def test_hdtime_seeding_summary_accepts_explicit_visual_separators(
    separator: str,
) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.params.get("action") is None:
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                ),
            )
        assert request.url.params["id"] == "99"
        assert request.url.params["action"] == "2"
        return httpx2.Response(
            200,
            text=f"<p><span>37 条记录</span>{separator}<strong>总大小：4.50 TiB</strong></p>",
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php", "/userdetails.php"]
    assert profile.seeding_count == 37
    assert profile.seeding_size_bytes == int(4.5 * 1024**4)


@pytest.mark.asyncio
async def test_hdtime_does_not_request_other_users_seeding_summary() -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">profile</a>'
                '<a href="userdetails.php?id=88&amp;action=2">当前做种</a>'
                '<a href="https://attacker.invalid/userdetails.php?id=99&amp;action=2">当前做种</a>'
                "<table><tr><td>等级</td><td>Member</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None
    assert paths == ["/index.php", "/userdetails.php"]


@pytest.mark.asyncio
async def test_hdtime_ambiguous_homepage_user_links_never_become_current_account() -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=88">OtherUser</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        assert request.url.path == "/usercp.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">settings</a>'
                '<a href="userdetails.php?id=88&amp;action=2">当前做种</a>'
                "<table><tr><td>等级</td><td>Member</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == [("/index.php", {}), ("/usercp.php", {})]
    assert profile.uid is None
    assert profile.username is None
    assert profile.user_level == "Member"
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
async def test_hdtime_ambiguous_homepage_follows_unique_profile_from_own_settings() -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append((request.url.path, dict(request.url.params)))
        assert request.method == "GET"
        assert request.url.host == "hdtime.org"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">当前账号设置</a>'
                    '<a href="userdetails.php?id=88">OtherUser</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/usercp.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">当前账号设置</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php" and "action" not in request.url.params:
            assert dict(request.url.params) == {"id": "99"}
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">当前账号设置</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        if request.url.path == "/userdetails.php":
            assert dict(request.url.params) == {"id": "99", "action": "2"}
            return httpx2.Response(200, text="<p>动态加载</p>")
        assert request.url.path == "/getusertorrentlistajax.php"
        assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
        return httpx2.Response(200, text="<div><b>37</b> 条记录 | 总大小：4.500 TB</div>")

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == [
        ("/index.php", {}),
        ("/usercp.php", {}),
        ("/userdetails.php", {"id": "99"}),
        ("/userdetails.php", {"id": "99", "action": "2"}),
        ("/getusertorrentlistajax.php", {"userid": "99", "type": "seeding"}),
    ]
    assert profile.uid == "99"
    assert profile.user_level == "Member"
    assert profile.seeding_count == 37
    assert profile.seeding_size_bytes == int(4.5 * 1024**4)


@pytest.mark.asyncio
@pytest.mark.parametrize("show_current_seeding", [True, False])
async def test_hdtime_settings_verified_current_seeding_field_without_action_link(
    show_current_seeding: bool,
) -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append((request.url.path, dict(request.url.params)))
        assert request.method == "GET"
        assert request.url.host == "hdtime.org"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">当前账号设置</a>'
                    '<a href="userdetails.php?id=88">OtherUser</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/usercp.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">当前账号设置</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            assert dict(request.url.params) == {"id": "99"}
            label = "<td>当前做种</td><td>查看统计</td>" if show_current_seeding else ""
            return httpx2.Response(
                200,
                text=(f'<a href="usercp.php">当前账号设置</a><table><tr>{label}</tr></table>'),
            )
        assert show_current_seeding
        assert request.url.path == "/getusertorrentlistajax.php"
        assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
        return httpx2.Response(200, text="<div><b>37</b> 条记录 | 总大小：4.500 TB</div>")

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    expected_paths = ["/index.php", "/usercp.php", "/userdetails.php"]
    if show_current_seeding:
        expected_paths.append("/getusertorrentlistajax.php")
        assert profile.seeding_count == 37
        assert profile.seeding_size_bytes == int(4.5 * 1024**4)
    else:
        assert profile.seeding_count is None
        assert profile.seeding_size_bytes is None
    assert [path for path, _ in requested] == expected_paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings_profile_links",
    [
        '<a href="https://other.invalid/userdetails.php?id=99">OtherOrigin</a>',
        (
            '<a href="userdetails.php?id=88">OtherUser</a>'
            '<a href="userdetails.php?id=99">CurrentUser</a>'
        ),
    ],
)
async def test_hdtime_untrusted_settings_links_never_authorize_seeding_ajax(
    settings_profile_links: str,
) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        assert request.url.host == "hdtime.org"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">当前账号设置</a>'
                    '<a href="userdetails.php?id=88">OtherUser</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        assert request.url.path == "/usercp.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">当前账号设置</a>'
                f"{settings_profile_links}"
                "<table><tr><td>当前做种</td><td>查看统计</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/usercp.php"]
    assert profile.uid is None
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
async def test_hdtime_repeated_same_user_link_does_not_block_profile() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        assert request.url.path == "/userdetails.php"
        assert dict(request.url.params) == {"id": "99"}
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">settings</a>'
                "<table><tr><td>等级</td><td>Member</td></tr></table>"
            ),
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php"]
    assert profile.uid == "99"
    assert profile.username == "CurrentUser"
    assert profile.user_level == "Member"


@pytest.mark.asyncio
async def test_hdtime_seeding_link_on_homepage_is_not_identity_evidence() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=88&amp;action=2">当前做种</a>'
                ),
            )
        assert request.url.path == "/usercp.php"
        return httpx2.Response(
            200,
            text='<a href="usercp.php">settings</a><p>当前账号设置页</p>',
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/usercp.php"]
    assert profile.uid is None
    assert profile.username is None
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
async def test_hdtime_homepage_user_identity_ignores_other_users_seeding_links() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=88&amp;action=2">当前做种</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        assert request.url.path == "/userdetails.php"
        assert dict(request.url.params) == {"id": "99"}
        return httpx2.Response(200, text='<a href="usercp.php">settings</a>')

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php"]
    assert profile.uid == "99"
    assert profile.username == "CurrentUser"


@pytest.mark.asyncio
async def test_hdtime_fetches_published_total_independently_of_seeding_summary() -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php" and "action" not in request.url.params:
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                    '<a href="userdetails.php?id=99&amp;action=1">发种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        if request.url.path == "/getusertorrentlistajax.php":
            assert dict(request.url.params) == {"userid": "99", "type": "uploaded"}
            return httpx2.Response(200, text="<p><b>7</b> 条记录 | 12.0 GiB</p>")
        assert request.url.path == "/userdetails.php"
        assert dict(request.url.params) == {"id": "99", "action": "2"}
        return httpx2.Response(200, text="<p>0 条记录 | 总大小：0 B</p>")

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()

    assert profile.torrents_posted == 7
    assert profile.seeding_count == 0
    assert profile.seeding_size_bytes == 0
    assert [path for path, _ in requested] == [
        "/index.php",
        "/userdetails.php",
        "/getusertorrentlistajax.php",
        "/userdetails.php",
    ]


@pytest.mark.asyncio
async def test_hdtime_rejects_ambiguous_seeding_summaries_without_guessing() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if not request.url.params.get("action"):
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                ),
            )
        return httpx2.Response(
            200,
            text="<p>5 条记录 | 总大小：3.0 GiB</p><p>15 条记录 | 总大小：9.0 GiB</p>",
        )

    profile = await HDTimeAdapter(
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
async def test_hhclub_hourly_bonus_from_explicit_same_origin_bonus_page() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "hhanclub.net"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="mybonus.php">bonus</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        assert request.url.path == "/mybonus.php"
        return httpx2.Response(
            200,
            text=("<div>你当前每小时能获取12.345个积分（A = 998.8）</div><div>合计 88.9</div>"),
        )

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php", "/mybonus.php"]
    assert profile.user_level == "Member"
    assert profile.bonus_per_hour == 12.345
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("没有记录", 0),
        ("No record.", 0),
        (
            "<div>17 | 2.4 TiB</div><table><tr><td>Title</td></tr></table>",
            17,
        ),
    ],
)
def test_nexusphp_upload_count_matches_pt_depiler_empty_and_aggregate_markers(
    html: str, expected: int
) -> None:
    assert _parse_nexus_ajax_total_count(html, _parse_html(html)) == expected


@pytest.mark.parametrize("html", ["没有记录", "No records.", "No record."])
def test_nexusphp_seeding_summary_treats_explicit_empty_marker_as_zero(html: str) -> None:
    assert _parse_nexus_ajax_seeding_summary(
        html,
        _parse_html(html),
        spring_sunday=True,
    ) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [37, 0])
async def test_hhclub_reads_current_users_full_published_count_from_confirmed_ajax(
    total: int,
) -> None:
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.url.path, dict(request.url.params)))
        assert request.method == "GET"
        assert request.url.host == "hhanclub.net"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php" and "action" not in request.url.params:
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99&amp;action=1">发种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        if request.url.path == "/userdetails.php":
            assert dict(request.url.params) == {"id": "99", "action": "1"}
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    "<p>合计 发种数量 发种体积</p>"
                    "<script>function loadPublished(type,page=0){"
                    "fetch(`getusertorrentlistajax.php?type=${type}&userid=99&ajax=1&page=${page}`)"
                    "}loadPublished('uploaded',0)</script>"
                ),
            )
        assert request.url.path == "/getusertorrentlistajax.php"
        assert dict(request.url.params) == {
            "type": "uploaded",
            "userid": "99",
            "ajax": "1",
            "page": "0",
        }
        rows = [{"title": "synthetic-only"}] if total else []
        return httpx2.Response(
            200,
            json={
                "data": rows,
                "total_count": total,
                "count": str(len(rows)),
                "page_num": 0,
                "total_offical_count": 7,
            },
        )

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "Member"
    assert profile.torrents_posted == total
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None
    assert requests == [
        ("/index.php", {}),
        ("/userdetails.php", {"id": "99"}),
        ("/userdetails.php", {"id": "99", "action": "1"}),
        (
            "/getusertorrentlistajax.php",
            {"type": "uploaded", "userid": "99", "ajax": "1", "page": "0"},
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "count": "0", "page_num": 0},
        {"data": [], "total_count": "12", "count": "0", "page_num": 0},
        {"data": [], "total_count": -1, "count": "0", "page_num": 0},
        {"data": [{"synthetic": True}], "total_count": 0, "count": "1", "page_num": 0},
        {"data": [], "total_count": 3, "count": "7", "page_num": 0},
        {"data": [], "total_count": 3, "count": "0", "page_num": "0"},
        {"data": "not-a-list", "total_count": 3, "count": "0", "page_num": 0},
    ],
)
def test_hhclub_published_ajax_rejects_ambiguous_or_partial_totals(
    payload: dict[str, object],
) -> None:
    from backend.app.infrastructure.adapters.nexusphp import _parse_hhclub_published_total

    assert _parse_hhclub_published_total(json.dumps(payload)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publication_href",
    [
        "userdetails.php?id=88&amp;action=1",
        "https://attacker.invalid/userdetails.php?id=99&amp;action=1",
    ],
)
async def test_hhclub_does_not_follow_other_account_or_cross_origin_publication(
    publication_href: str,
) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hhanclub.net"
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        assert request.url.path == "/userdetails.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">settings</a>'
                f'<a href="{publication_href}">发种</a>'
                "<table><tr><td>等级</td><td>Member</td></tr></table>"
            ),
        )

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php"]
    assert profile.user_level == "Member"
    assert profile.torrents_posted is None


@pytest.mark.asyncio
async def test_hhclub_publication_page_cannot_authorize_ajax_for_a_different_uid() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php" and "action" not in request.url.params:
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99&amp;action=1">发种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        assert request.url.path == "/userdetails.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">settings</a>'
                "<p>合计 发种数量 发种体积</p>"
                "<script>function loadPublished(type,page=0){"
                "fetch(`getusertorrentlistajax.php?type=${type}&userid=88&ajax=1&page=${page}`)"
                "}loadPublished('uploaded',0)</script>"
            ),
        )

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php", "/userdetails.php"]
    assert profile.user_level == "Member"
    assert profile.torrents_posted is None


@pytest.mark.asyncio
async def test_hhclub_optional_publication_ajax_failure_keeps_main_profile() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php" and "action" not in request.url.params:
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99&amp;action=1">发种</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    "<p>合计 发种数量 发种体积</p>"
                    "<script>function loadPublished(type,page=0){"
                    "fetch(`getusertorrentlistajax.php?type=${type}&userid=99&ajax=1&page=${page}`)"
                    "}loadPublished('uploaded',0)</script>"
                ),
            )
        assert request.url.path == "/getusertorrentlistajax.php"
        return httpx2.Response(403)

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert requested == [
        "/index.php",
        "/userdetails.php",
        "/userdetails.php",
        "/getusertorrentlistajax.php",
    ]
    assert profile.user_level == "Member"
    assert profile.torrents_posted is None


@pytest.mark.asyncio
async def test_hhclub_seeding_categories_are_not_assumed_to_be_full_totals() -> None:
    """Distinct ordinary/official buckets are not a verified all-seeding snapshot."""
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        assert request.url.host == "hhanclub.net"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=99">CurrentUser</a>'
                ),
            )
        assert request.url.path == "/userdetails.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">settings</a>'
                '<a href="userdetails.php?id=99&amp;action=2">当前做种</a>'
                "<table><tr><td>等级</td><td>Member</td></tr></table>"
                "<div>普通保种数量 12；官种数量 4；普通保种体积 80 GiB；官种体积 25 GiB</div>"
            ),
        )

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php"]
    assert profile.user_level == "Member"
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None
    assert profile.seeding_points is None


@pytest.mark.asyncio
async def test_hhclub_optional_bonus_page_failure_preserves_main_profile() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">profile</a>'
                    '<a href="mybonus.php">bonus</a>'
                    "<table><tr><td>等级</td><td>Member</td></tr></table>"
                ),
            )
        assert request.url.path == "/mybonus.php"
        return httpx2.Response(404)

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php", "/mybonus.php"]
    assert profile.user_level == "Member"
    assert profile.bonus_per_hour is None


@pytest.mark.asyncio
async def test_hhclub_profile_does_not_follow_cross_origin_bonus_link() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.host or "")
        assert request.url.host == "hhanclub.net"
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text='<a href="usercp.php">profile</a><a href="userdetails.php?id=99">User</a>',
            )
        assert request.url.path == "/userdetails.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">profile</a>'
                '<a href="https://attacker.invalid/mybonus.php">bonus</a>'
                "<table><tr><td>等级</td><td>Member</td></tr></table>"
            ),
        )

    profile = await HHClubAdapter(
        _COOKIE, transport=_hhclub_transport(handler)
    ).fetch_user_profile()
    assert profile.bonus_per_hour is None
    assert requested == ["hhanclub.net", "hhanclub.net"]


@pytest.mark.asyncio
async def test_hdtime_rejects_login_redirect_and_login_page() -> None:
    def redirect_handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "/login.php"})

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(redirect_handler))
    with pytest.raises(SiteAdapterError) as redirect_failure:
        await adapter.test_connection()
    assert redirect_failure.value.code == "SITE_AUTH_FAILED"

    def login_handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text='<a href="login.php">login</a>')

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(login_handler))
    with pytest.raises(SiteAdapterError) as page_failure:
        await adapter.search(SearchQuery(("movie",), SearchMediaType.MOVIE))
    assert page_failure.value.code == "SITE_AUTH_FAILED"


@pytest.mark.asyncio
async def test_hdtime_never_sends_cookie_to_cross_origin_download() -> None:
    hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host or "")
        return httpx2.Response(
            200,
            text=(
                "<h1>Synthetic.Movie.2026</h1>"
                '<a href="https://attacker.invalid/download.php?id=123">download</a>'
            ),
        )

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")
    assert failure.value.code == "SITE_DOWNLOAD_URL_INVALID"
    assert hosts == ["hdtime.org"]
    assert "attacker.invalid" not in str(failure.value)


@pytest.mark.asyncio
async def test_hdtime_rejects_malformed_download_origin_with_stable_error() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            text=(
                "<h1>Synthetic.Movie.2026</h1>"
                '<a href="https://hdtime.org:invalid/download.php?id=123">download</a>'
            ),
        )

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")
    assert failure.value.code == "SITE_DOWNLOAD_URL_INVALID"


@pytest.mark.asyncio
async def test_hdtime_enforces_html_and_torrent_size_limits() -> None:
    def html_handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * 9)

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(html_handler), max_html_bytes=8)
    with pytest.raises(SiteAdapterError) as html_failure:
        await adapter.test_connection()
    assert html_failure.value.code == "SITE_RESPONSE_TOO_LARGE"

    def torrent_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/details.php":
            return httpx2.Response(200, text=_details_html())
        return httpx2.Response(200, content=b"d" + b"x" * 8)

    adapter = HDTimeAdapter(
        _COOKIE,
        transport=httpx2.MockTransport(torrent_handler),
        max_torrent_bytes=8,
    )
    with pytest.raises(SiteAdapterError) as torrent_failure:
        await adapter.fetch_torrent("123")
    assert torrent_failure.value.code == "SITE_RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_hdtime_rejects_non_bencoded_download_body() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/details.php":
            return httpx2.Response(200, text=_details_html())
        return httpx2.Response(200, content=b"<html>login</html>")

    adapter = HDTimeAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")
    assert failure.value.code == "SITE_INVALID_RESPONSE"


def test_hdtime_cookie_and_origin_validation_rejects_unsafe_inputs() -> None:
    with pytest.raises(ValueError):
        HDTimeAdapter("bad\r\ncookie")
    with pytest.raises(ValueError):
        HDTimeAdapter(_COOKIE, base_url="http://hdtime.org")
    with pytest.raises(ValueError):
        HDTimeAdapter(_COOKIE, base_url="https://user:pass@hdtime.org")


@pytest.mark.asyncio
async def test_hhclub_card_layout_search_and_download_use_exact_script_names() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.host == "hhanclub.net"
        assert request.headers.get("cookie") == _COOKIE
        if request.url.path == "/index.php":
            return httpx2.Response(200, text='<a href="usercp.php">profile</a>')
        if request.url.path == "/torrents.php":
            return httpx2.Response(200, text=_hhclub_search_html())
        if request.url.path == "/details.php":
            return httpx2.Response(200, text=_hhclub_details_html())
        if request.url.path == "/download.php":
            return httpx2.Response(200, content=_TORRENT)
        return httpx2.Response(404)

    adapter = HHClubAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    await assert_read_only_site_adapter_contract(
        SiteAdapterContractCase(
            adapter,
            SearchQuery(("Synthetic", "Movie"), SearchMediaType.MOVIE),
            "hhclub",
            "456",
        )
    )
    page = await adapter.search(SearchQuery(("Synthetic", "Movie"), SearchMediaType.MOVIE))
    assert len(page.items) == 1
    candidate = page.items[0]
    assert candidate.torrent_id == "456"
    assert candidate.total_size == int(42.35 * 1024**3)
    assert candidate.category == "401"
    assert candidate.seeders == 80
    assert candidate.leechers == 0
    assert all(request.url.path != "/userdetails.php" for request in requests)


@pytest.mark.asyncio
async def test_hhclub_torrent_timeout_maps_to_retryable_site_timeout() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("synthetic timeout")

    adapter = HHClubAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("456")

    assert failure.value.code == "SITE_TIMEOUT"
    assert failure.value.retryable is True
    assert "synthetic-secret" not in str(failure.value)


def test_hhclub_origin_validation_rejects_old_or_unrelated_hosts() -> None:
    with pytest.raises(ValueError):
        HHClubAdapter(_COOKIE, base_url="http://hhanclub.net")
    with pytest.raises(ValueError):
        HHClubAdapter(_COOKIE, base_url="https://example.com")


def _search_html(*, empty: bool = False) -> str:
    if empty:
        return '<html><a href="usercp.php">profile</a><table class="torrents"></table></html>'
    return """
    <html>
      <a href="usercp.php">profile</a>
      <table class="torrents"><tbody>
        <tr>
          <td><table class="torrentname"><tr><td>
            <a href="details.php?id=123"
               title="Synthetic.Movie.2026.2160p.WEB-DL.HEVC">Synthetic</a>
            <a href="?cat=401">Movie</a>
            <a href="https://www.imdb.com/title/tt1234567/">IMDb</a>
            <a href="https://movie.douban.com/subject/1295644/">Douban</a>
          </td></tr></table></td>
          <td>files</td><td>comments</td><td>owner</td>
          <td><span title="2026-09-09 12:00:00">today</span></td>
          <td>4.00 GiB</td><td>8</td><td>2</td><td>10</td><td>status</td>
        </tr>
      </tbody></table>
      <a href="torrents.php?search=Synthetic&amp;page=1">next</a>
    </html>
    """


def _details_html() -> str:
    return """
    <html><head><title>HDTIME :: Synthetic.Movie.2026</title></head>
      <body><a href="usercp.php">profile</a><h1>Synthetic.Movie.2026.2160p.WEB-DL.HEVC</h1>
      <a href="https://www.imdb.com/title/tt1234567/">IMDb</a>
      <a href="download.php?id=123">download</a></body>
    </html>
    """


def _hhclub_search_html() -> str:
    return """
    <html><body>
      <a href="usercp.php">profile</a>
      <a href="userdetails.php?id=99">not a torrent detail</a>
      <div class="torrent-table-sub-info">
        <a href="?cat[]=401">Movie</a>
        <div class="torrent-title">
          <a class="torrent-info-text-name" href="details.php?id=456&amp;hit=1">
            Synthetic.Movie.2026.2160p.WEB-DL.HEVC
          </a>
        </div>
        <div class="torrent-info-text torrent-info-text-size">42.35 GB</div>
        <div class="torrent-info-text torrent-info-text-added">3天</div>
        <div class="torrent-info-text torrent-info-text-seeders">
          <a href="details.php?id=456&amp;hit=1&amp;dllist=1#seeders">80</a>
        </div>
        <div class="torrent-info-text torrent-info-text-leechers">0</div>
        <a href="download.php?id=456&amp;passkey=synthetic">下载</a>
      </div>
    </body></html>
    """


def _hhclub_details_html() -> str:
    return """
    <html><head><title>
      HHCLUB :: 种子详情 "Synthetic.Movie.2026" - Powered by NexusPHP
    </title></head><body>
      <a href="usercp.php">profile</a>
      <a href="userdetails.php?id=99">owner</a>
      <a href="download.php?id=456&amp;passkey=synthetic">download</a>
    </body></html>
    """


def test_user_level_parser_prefers_title_and_never_uses_neighboring_sidebar_text() -> None:
    parser = _parse_html(
        '<table><tr><td>用户等级</td><td><img title="Power User" alt="种子评论"></td></tr></table>'
        '<a href="comments.php">种子评论</a><a href="experience.php">经验值</a>'
    )
    assert _profile_named_level(parser) == "Power User"

    empty_grade = _parse_html(
        "<table><tr><td>用户等级</td><td></td></tr></table>"
        '<a href="comments.php">种子评论</a><a href="experience.php">经验值</a>'
    )
    assert _profile_named_level(empty_grade) is None

    legacy_grade_note = _parse_html(
        "<table><tr><td>等级</td><td>(幼儿园)User 🩺校验等级 "
        "加群参考《规则&教程》第二部分第4条</td></tr></table>"
    )
    assert _profile_named_level(legacy_grade_note) == "(幼儿园)User"


def test_hhclub_user_level_comes_from_profile_link_class() -> None:
    parser = _parse_html('<a href="userdetails.php?id=99" class="Power_User_Name">YYxiaoma</a>')
    assert _profile_named_level(parser, site_id="hhclub") == "Power User"


def test_audiences_user_level_comes_from_profile_hero_class() -> None:
    parser = _parse_html(
        '<div class="ud-hero"><span class="ud-class"><b>精英(Elite)</b></span></div>'
        '<a href="comments.php">种子评论</a><a href="experience.php">经验值</a>'
    )
    assert _profile_named_level(parser, site_id="audiences") == "精英(Elite)"


@pytest.mark.asyncio
async def test_u2_uses_its_bonus_and_seeding_summary_endpoints() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><a href="userdetails.php?id=99">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><table>'
                    '<tr><td>用户等级</td><td><b title="旅人"></b></td></tr>'
                    "<tr><td>上传量</td><td>55.17 GiB</td></tr>"
                    "<tr><td>下载量</td><td>0 B</td></tr>"
                    "<tr><td>分享率</td><td>∞</td></tr>"
                    '<tr><td>UCoin</td><td><span title="26,191.39">UCoin</span></td></tr>'
                    "<tr><td>做种积分</td><td>616,255.50</td></tr>"
                    "<tr><td>发种数</td><td>0</td></tr>"
                    "</table>"
                ),
            )
        if request.url.path == "/mprecent.php":
            assert request.url.params.get("user") == "99"
            return httpx2.Response(
                200, text='<table class="main"><td class="text">UCoin240</td></table>'
            )
        if request.url.path == "/getusertorrentlistajax.php":
            assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
            return httpx2.Response(
                200,
                text=(
                    "<div><b>17</b>条记录 / 大小 28.55 GiB</div>"
                    "<table><tr><td>Title</td></tr></table>"
                ),
            )
        raise AssertionError(f"Unexpected U2 request: {request.url}")

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="u2",
            default_base_url="https://u2.dmhy.org",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.user_level == "旅人"
    assert profile.uploaded_bytes == int(55.17 * 1024**3)
    assert profile.downloaded_bytes == 0
    assert profile.ratio is None  # UI formats uploaded > 0 / downloaded == 0 as ∞
    assert profile.bonus == 26191.39
    assert profile.seeding_points == 616255.50
    assert profile.bonus_per_hour == 10.0
    assert profile.seeding_count == 17
    assert profile.seeding_size_bytes == int(28.55 * 1024**3)
    assert profile.torrents_posted == 0
    assert [request.url.path for request in requests] == [
        "/index.php",
        "/userdetails.php",
        "/mprecent.php",
        "/getusertorrentlistajax.php",
    ]


@pytest.mark.asyncio
async def test_ubits_profile_uses_site_specific_coin_and_seed_point_labels() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<a class="Name" href="userdetails.php?id=99&hash=synthetic">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/mybonus.php":
            return httpx2.Response(
                200,
                text=('<a href="usercp.php">settings</a><div>你当前每小时能获取 17.25</div>'),
            )
        assert request.url.path == "/userdetails.php"
        assert request.url.params.get("id") == "99"
        assert "hash" not in request.url.params
        return httpx2.Response(
            200,
            text=(
                '<a href="usercp.php">settings</a><table>'
                '<tr><td>用户等级</td><td><img title="Power User" alt="Power User"></td></tr>'
                "<tr><td>上传量</td><td>3.03 TiB</td></tr>"
                "<tr><td>下载量</td><td>509 GiB</td></tr>"
                "<tr><td>分享率</td><td>6.09</td></tr>"
                "<tr><td>U币</td><td>340,160.90</td></tr>"
                "<tr><td>做种积分</td><td>82,200.10</td></tr>"
                "<tr><td>做种数</td><td>475</td></tr>"
                "<tr><td>做种大小</td><td>13.68 TiB</td></tr>"
                "<tr><td>发种数</td><td>6</td></tr>"
                "</table>"
            ),
        )

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="ubits",
            default_base_url="https://ubits.club",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.user_level == "Power User"
    assert profile.uploaded_bytes == int(3.03 * 1024**4)
    assert profile.downloaded_bytes == 509 * 1024**3
    assert profile.ratio == 6.09
    assert profile.bonus == 340160.90
    assert profile.bonus_per_hour == 17.25
    assert profile.seeding_points == 82200.10
    assert profile.seeding_count == 475
    assert profile.seeding_size_bytes == int(13.68 * 1024**4)
    assert profile.torrents_posted == 6


@pytest.mark.asyncio
async def test_pttime_uses_legacy_getusertorrentlist_endpoint_for_totals() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><a href="userdetails.php?id=99">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><table>'
                    '<tr><td>用户等级</td><td><b title="幼儿园 User"></b></td></tr>'
                    "<tr><td>上传量</td><td>690.17 GiB</td></tr>"
                    "<tr><td>下载量</td><td>500 GiB</td></tr>"
                    "<tr><td>分享率</td><td>1.38</td></tr>"
                    "</table>"
                ),
            )
        if request.url.path == "/getusertorrentlist.php":
            if request.url.params.get("type") == "seeding":
                return httpx2.Response(
                    200,
                    text=(
                        '<div id="outer"><span>Info</span><span>Info</span>'
                        "<span><b>2</b></span><span>4.94 TiB</span></div>"
                        "<table><tr><td>Title</td></tr><tr><td>one</td></tr></table>"
                    ),
                )
            assert request.url.params.get("type") == "uploaded"
            return httpx2.Response(
                200,
                text="<div><b>13</b>条记录</div><table><tr><td>Title</td></tr></table>",
            )
        raise AssertionError(f"Unexpected PTTime request: {request.url}")

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="pttime",
            default_base_url="https://www.pttime.org",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.user_level == "幼儿园 User"
    assert profile.seeding_count == 2
    assert profile.seeding_size_bytes == int(4.94 * 1024**4)
    assert profile.torrents_posted == 13
    assert [request.url.path for request in requests] == [
        "/index.php",
        "/userdetails.php",
        "/getusertorrentlist.php",
        "/getusertorrentlist.php",
    ]


@pytest.mark.asyncio
async def test_audiences_profile_reads_mybonus_and_no_record_upload_response() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><a href="userdetails.php?id=99">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<div class="ud-hero"><span class="ud-class">'
                    '<b class="Power_User_Name">YYxiaoma</b></span></div>'
                    '<div class="site-userbar__compact-metric--uploaded">上传量 717.12 GiB</div>'
                    '<div class="site-userbar__compact-metric--downloaded">下载量 477.60 GiB</div>'
                    '<div class="site-userbar__compact-metric--ratio">1.51</div>'
                    '<div class="site-userbar__compact-metric--bonus">爆米花 23,456.70</div>'
                    '<div class="site-userbar__compact-metric--seeding-bonus">'
                    "做种积分 45,678.90</div>"
                    '<a class="site-userbar__compact-metric-inline-link--seeding">405</a>'
                    "<table>"
                    '<tr><td>用户等级</td><td><b title="User"></b></td></tr>'
                    "<tr><td>上传量</td><td>1 GiB</td></tr>"
                    "<tr><td>下载量</td><td>2 GiB</td></tr>"
                    "<tr><td>分享率</td><td>9.99</td></tr></table>"
                ),
            )
        if request.url.path == "/mybonus.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a>'
                    '<div class="mybonus-side__rate">你当前每小时能获取 12.75</div>'
                ),
            )
        assert request.url.path == "/getusertorrentlistajax.php"
        assert request.headers.get("referer") == "https://audiences.me/userdetails.php"
        if request.url.params.get("type") == "seeding":
            return httpx2.Response(
                200,
                text=(
                    "<table><tr><td>Title</td><td>Count</td><td>Size</td></tr>"
                    "<tr><td>Total</td><td>404</td><td>5.25 TiB</td></tr></table>"
                ),
            )
        return httpx2.Response(200, text="没有记录")

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="audiences",
            default_base_url="https://audiences.me",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.user_level == "Power User"
    assert profile.uploaded_bytes == int(717.12 * 1024**3)
    assert profile.downloaded_bytes == int(477.60 * 1024**3)
    assert profile.ratio == 1.51
    assert profile.bonus == 23456.70
    assert profile.seeding_points == 45678.90
    assert profile.bonus_per_hour == 12.75
    assert profile.seeding_count == 405  # profile compact metric wins over a list summary
    assert profile.seeding_size_bytes == int(5.25 * 1024**4)
    assert profile.torrents_posted == 0
    assert "/mybonus.php" in [r.url.path for r in requests]
    assert len([r for r in requests if r.url.path == "/getusertorrentlistajax.php"]) == 2


@pytest.mark.asyncio
async def test_keepfrds_reads_user_and_torrent_stats_from_json_api_without_retired_ajax() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><a href="userdetails.php?id=99">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><table>'
                    "<tr><td>实际上传量</td><td>2.00 TiB</td></tr>"
                    "<tr><td>实际下载量</td><td>1.00 TiB</td></tr></table>"
                ),
            )
        assert request.url.path == "/api/userdetails.php"
        assert "application/json" in request.headers.get("accept", "")
        return httpx2.Response(
            200,
            json={
                "version": 1,
                "user": {
                    "id": 99,
                    "username": "YYxiaoma",
                    "class": 2,
                    "joinedAt": "2024-01-15T10:30:22Z",
                    "uploadedBytes": 3 * 1024**4,
                    "downloadedBytes": 512 * 1024**3,
                    "bonus": 12345.6,
                },
                "torrentStats": {
                    "seeding": 607,
                    "seedingBytes": 13 * 1024**4,
                    "leeching": 0,
                    "uploaded": 9,
                    "updatedAt": "2026-10-19T00:00:00Z",
                },
            },
        )

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="keepfrds",
            default_base_url="https://pt.keepfrds.com",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.uid == "99"
    assert profile.username == "YYxiaoma"
    assert profile.user_level == "Power User"
    assert profile.uploaded_bytes == 3 * 1024**4
    assert profile.downloaded_bytes == 512 * 1024**3
    assert profile.ratio == 6.0
    assert profile.torrents_posted == 9
    assert profile.seeding_count == 607
    assert profile.seeding_size_bytes == 13 * 1024**4
    assert profile.bonus == 12345.6
    assert profile.join_time is not None
    assert not any(request.url.path == "/getusertorrentlistajax.php" for request in requests)


@pytest.mark.asyncio
async def test_pterclub_uses_profile_seeding_count_and_site_specific_published_queries() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<span id="info_block"><div><a href="usercp.php">settings</a></div>'
                    '<a href="userdetails.php?id=99"></a></span>'
                    '<a href="userdetails.php?id=100">OtherUser</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<span id="info_block"><div>profile</div>'
                    '<a href="userdetails.php?id=99">YYxiaoma</a></span>'
                    '<div style="background: red"><a href="messages.php">6</a></div>'
                    '<a href="getusertorrentlist.php?userid=99&amp;type=seeding">607</a>'
                    "<table>"
                    '<tr><td>用户等级</td><td><img title="伯曼猫 User" alt="伯曼猫 User"></td></tr>'
                    "<tr><td>Join date</td><td>2020-03-04 05:06:07</td></tr>"
                    "<tr><td>最近動向</td><td>2026-11-04 12:13:14 (today)</td></tr>"
                    "<tr><td>猫粮</td><td>1,234.50</td></tr>"
                    "<tr><td>上传量</td><td>886.73 GiB</td></tr>"
                    "<tr><td>下载量</td><td>1.66 TiB</td></tr>"
                    "<tr><td>分享率</td><td>534.17</td></tr>"
                    "<tr><td>发种数</td><td>9</td></tr>"
                    "</table>"
                ),
            )
        if request.url.path == "/mybonus.php":
            timeout = request.extensions.get("timeout")
            assert isinstance(timeout, dict)
            assert timeout.get("read") == 60.0
            return httpx2.Response(
                200,
                text='<table id="outer"><tr><td rowspan="2">12.5</td></tr></table>',
            )
        if request.url.path == "/getusertorrentlistajax.php":
            assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
            return httpx2.Response(
                200,
                text=(
                    "<div><b>12</b>条记录 / 总大小 <b>7.52 TiB</b></div>"
                    "<table><tr><td>Title</td></tr></table>"
                ),
            )
        assert request.url.path == "/torrents.php"
        params = dict(request.url.params)
        assert params["search"] == "YYxiaoma"
        assert params["search_area"] == "3"
        assert params["search_mode"] == "3"
        assert params["incldead"] == "1"
        assert params["spstate"] == "0"
        assert params["inclbookmarked"] == "0"
        if params["check"] == "prechecked":
            return httpx2.Response(
                200,
                text='<p class="np-pager"><b>1</b><b>17</b></p>',
            )
        assert params["check"] == "checked"
        return httpx2.Response(
            200,
            text='<p class="np-pager"><b>1</b><b>5</b></p>',
        )

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="pterclub",
            default_base_url="https://pterclub.net",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.uid == "99"
    assert profile.username == "YYxiaoma"
    assert profile.user_level == "伯曼猫 User"
    assert profile.join_time is not None
    assert profile.join_time.strftime("%Y-%m-%dT%H:%M:%S") == "2020-03-04T05:06:07"
    assert profile.last_access_at is not None
    assert profile.last_access_at.strftime("%Y-%m-%dT%H:%M:%S") == "2026-11-04T12:13:14"
    assert profile.message_count == 6
    assert profile.bonus == 1234.5
    assert profile.bonus_per_hour == 12.5
    assert profile.seeding_count == 607
    assert profile.seeding_size_bytes == int(7.52 * 1024**4)
    assert profile.torrents_posted == 22  # prechecked + checked, not generic AJAX total
    assert [request.url.path for request in requests] == [
        "/index.php",
        "/userdetails.php",
        "/mybonus.php",
        "/getusertorrentlistajax.php",
        "/torrents.php",
        "/torrents.php",
    ]


@pytest.mark.asyncio
async def test_hdhome_profile_uses_custom_hourly_bonus_and_uploaded_ajax_total() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<div id="info_block"><a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=88">HDUser</a></div>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<div id="info_block"><a href="userdetails.php?id=88">HDUser</a></div>'
                    '<table><tr><td class="text"><h1>魔力统计</h1>'
                    '<div align="center">1</div><div align="center">2</div>'
                    '<div align="center">3</div><div align="center">4</div>'
                    '<div align="center">5</div><div align="center">6</div>'
                    '<div align="center">7</div><div align="center">8</div>'
                    '<div align="center">9</div><div align="center">10</div>'
                    "</td></tr></table>"
                ),
            )
        assert request.url.path == "/getusertorrentlistajax.php"
        params = dict(request.url.params)
        assert params["userid"] == "88"
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        assert timeout.get("read") == 60.0
        if params["type"] == "seeding":
            return httpx2.Response(
                200,
                text="<div><b>5</b>条记录 / 总大小 <b>1.5 TiB</b></div><table></table>",
            )
        assert params["type"] == "uploaded"
        return httpx2.Response(
            200,
            text="<div><b>31</b>条记录</div><table><tr><td>Title</td></tr></table>",
        )

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="hdhome",
            default_base_url="https://hdhome.org",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.uid == "88"
    assert profile.username == "HDUser"
    assert profile.bonus_per_hour == 16.0
    assert profile.seeding_count == 5
    assert profile.seeding_size_bytes == int(1.5 * 1024**4)
    assert profile.torrents_posted == 31
    assert [request.url.path for request in requests] == [
        "/index.php",
        "/userdetails.php",
        "/getusertorrentlistajax.php",
        "/getusertorrentlistajax.php",
    ]


@pytest.mark.asyncio
async def test_hdhome_uses_mybonus_hourly_rate_when_profile_block_is_absent() -> None:
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<div id="info_block"><a href="usercp.php">settings</a>'
                    '<a href="userdetails.php?id=88">HDUser</a></div>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<div id="info_block"><a href="userdetails.php?id=88">HDUser</a></div>'
                    "<table><tr><td>用户等级</td><td>Power User</td></tr></table>"
                ),
            )
        if request.url.path == "/mybonus.php":
            return httpx2.Response(
                200,
                text="<div>你当前每小时能获取100.741个魔力值</div>",
            )
        if request.url.path == "/getusertorrentlistajax.php":
            assert request.url.params.get("userid") == "88"
            return httpx2.Response(200, text="没有记录")
        raise AssertionError(f"Unexpected HDHome request: {request.url}")

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="hdhome",
            default_base_url="https://hdhome.org",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.uid == "88"
    assert profile.username == "HDUser"
    assert profile.bonus_per_hour == 100.741
    assert profile.seeding_count == 0
    assert profile.seeding_size_bytes == 0
    assert profile.torrents_posted == 0
    assert [path for path, _params in requests] == [
        "/index.php",
        "/userdetails.php",
        "/mybonus.php",
        "/getusertorrentlistajax.php",
        "/getusertorrentlistajax.php",
    ]


@pytest.mark.asyncio
async def test_hhclub_profile_parses_site_specific_bonus_and_generic_seeding_total() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><a href="userdetails.php?id=99">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><table>'
                    "<tr><td>等级</td><td>Power User</td></tr>"
                    "<tr><td>上传量</td><td>3.15 TiB</td></tr>"
                    "<tr><td>下载量</td><td>694 GiB</td></tr>"
                    "<tr><td>分享率</td><td>4.65</td></tr>"
                    "<tr><td>憨豆</td><td>313,451.00</td></tr>"
                    "<tr><td>做种积分</td><td>102,358.60</td></tr>"
                    '</table><div class="grid"><div class="row-span-4">12.75</div></div>'
                ),
            )
        assert request.url.path == "/getusertorrentlistajax.php"
        assert dict(request.url.params) == {"userid": "99", "type": "seeding"}
        return httpx2.Response(
            200,
            text=(
                "<div><b>694</b>条记录 / 总大小 <b>14.95 TiB</b></div>"
                "<table><tr><td>Title</td></tr></table>"
            ),
        )

    profile = await HHClubAdapter(
        _COOKIE,
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.user_level == "Power User"
    assert profile.uploaded_bytes == int(3.15 * 1024**4)
    assert profile.downloaded_bytes == 694 * 1024**3
    assert profile.ratio == 4.65
    assert profile.bonus == 313451.0
    assert profile.seeding_points == 102358.6
    assert profile.bonus_per_hour == 12.75
    assert profile.seeding_count == 694
    assert profile.seeding_size_bytes == int(14.95 * 1024**4)
    assert [request.url.path for request in requests] == [
        "/index.php",
        "/userdetails.php",
        "/getusertorrentlistajax.php",
    ]


@pytest.mark.asyncio
async def test_springsunday_total_timeout_preserves_profile_on_slow_uploaded_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.infrastructure.adapters.nexusphp._DEFAULT_PROFILE_TOTAL_TIMEOUT_SECONDS",
        0.01,
    )
    requests: list[tuple[str, str | None]] = []

    class SlowUploadedTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            path = request.url.path
            request_type = request.url.params.get("type")
            requests.append((path, request_type))
            if path == "/index.php":
                return httpx2.Response(
                    200,
                    text=(
                        '<a href="usercp.php">settings</a>'
                        '<a href="userdetails.php?id=99">YYxiaoma</a>'
                    ),
                )
            if path == "/userdetails.php":
                return httpx2.Response(
                    200,
                    text=(
                        '<a href="usercp.php">settings</a><table>'
                        "<tr><td>用户等级</td><td>User</td></tr>"
                        "<tr><td>上传量</td><td>10 GiB</td></tr>"
                        "<tr><td>下载量</td><td>5 GiB</td></tr>"
                        "<tr><td>分享率</td><td>2.0</td></tr>"
                        "<tr><td>积分</td><td>100</td></tr></table>"
                    ),
                )
            if path == "/mybonus.php":
                return httpx2.Response(200, text="<table></table>")
            if path == "/getusertorrentlistajax.php" and request_type == "seeding":
                return httpx2.Response(
                    200,
                    text=(
                        "<div><b>5</b>条记录 / 总大小 <b>1.0 TiB</b></div>"
                        "<table><tr><td>Title</td></tr></table>"
                    ),
                )
            if path == "/getusertorrentlistajax.php" and request_type == "uploaded":
                await asyncio.sleep(0.1)
                return httpx2.Response(200, text="<div><b>0</b>条记录</div>")
            raise AssertionError(f"Unexpected SpringSunday request: {request.url}")

        async def aclose(self) -> None:
            return None

    adapter = NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="spring_sunday",
            default_base_url="https://springsunday.net",
            min_request_interval_seconds=0,
        ),
        transport=SlowUploadedTransport(),
        timeout_seconds=0.001,
    )
    profile = await adapter.fetch_user_profile()

    assert profile.user_level == "User"
    assert profile.uploaded_bytes == 10 * 1024**3
    assert profile.downloaded_bytes == 5 * 1024**3
    assert profile.seeding_count == 5
    assert profile.seeding_size_bytes == 1024**4
    assert profile.torrents_posted is None
    assert requests.count(("/index.php", None)) == 1
    assert requests.count(("/userdetails.php", None)) == 1
    assert requests.count(("/mybonus.php", None)) == 1
    assert requests.count(("/getusertorrentlistajax.php", "seeding")) == 1
    assert requests.count(("/getusertorrentlistajax.php", "uploaded")) == 1


@pytest.mark.asyncio
async def test_springsunday_optional_stat_timeout_does_not_retry_whole_profile() -> None:
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><a href="userdetails.php?id=99">YYxiaoma</a>'
                ),
            )
        if request.url.path == "/userdetails.php":
            return httpx2.Response(
                200,
                text=(
                    '<a href="usercp.php">settings</a><table>'
                    "<tr><td>上传量</td><td>644.07 GiB</td></tr>"
                    "<tr><td>下载量</td><td>0 B</td></tr>"
                    "<tr><td>分享率</td><td>∞</td></tr>"
                    "<tr><td>积分</td><td>10,577.70</td></tr></table>"
                ),
            )
        if request.url.path == "/mybonus.php":
            return httpx2.Response(200, text="<table></table>")
        if request.url.path == "/getusertorrentlistajax.php":
            raise httpx2.ReadTimeout("synthetic slow optional endpoint")
        raise AssertionError(f"Unexpected SpringSunday request: {request.url}")

    profile = await NexusPhpWebAdapter(
        _COOKIE,
        profile=NexusPhpProfile(
            site_id="spring_sunday",
            default_base_url="https://springsunday.net",
            min_request_interval_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    ).fetch_user_profile()

    assert profile.uploaded_bytes == int(644.07 * 1024**3)
    assert profile.downloaded_bytes == 0
    assert profile.bonus == 10577.70
    assert requests.count("/index.php") == 1
    assert requests.count("/userdetails.php") == 1
    assert requests.count("/mybonus.php") == 1
    assert requests.count("/getusertorrentlistajax.php") == 2
