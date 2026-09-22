from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx2
import pytest

from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_search import SearchMediaType, SearchQuery, SearchSortHint
from backend.app.infrastructure.adapters.nexusphp import HDTimeAdapter, HHClubAdapter
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from tests.contract.site_adapter_contract import (
    SiteAdapterContractCase,
    assert_read_only_site_adapter_contract,
)

_COOKIE = "uid=synthetic; pass=synthetic-secret"
_TORRENT = b"d4:infod4:name9:syntheticee"


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
    assert seen_query[0]["page"] == ["2"]
    assert seen_query[0]["sort"] == ["7"]


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
        _COOKIE, transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert requested == ["/index.php", "/userdetails.php", "/mybonus.php"]
    assert profile.user_level == "Member"
    assert profile.bonus_per_hour == 12.345
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
        _COOKIE, transport=httpx2.MockTransport(handler)
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
async def test_hhclub_torrent_timeout_maps_to_retryable_site_unavailable() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("synthetic timeout")

    adapter = HHClubAdapter(_COOKIE, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("456")

    assert failure.value.code == "SITE_UNAVAILABLE"
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
