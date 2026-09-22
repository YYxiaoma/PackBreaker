from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2
import pytest

from backend.app.domain.site_adapter import (
    SiteConnectionResult,
    SiteUserProfile,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_config import SiteCredentialKind, SiteKind, site_profile
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.sites import (
    MTeamAdapter,
    SiteAdapterError,
    SiteAdapterFactory,
)
from tests.contract.site_adapter_contract import (
    SiteAdapterContractCase,
    assert_read_only_site_adapter_contract,
)

_TORRENT_BYTES = b"d4:infod4:name9:syntheticee"
_VALID_TORRENT_BYTES = (
    b"d4:infod6:lengthi1e4:name9:synthetic12:piece lengthi16384e6:pieces20:01234567890123456789ee"
)


class FakeSiteAdapter:
    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(supports_pagination=True, requires_download_token=False)

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

    async def fetch_user_profile(self) -> SiteUserProfile:
        return SiteUserProfile("fake", uid="1", username="synthetic")

    async def search(self, query: SearchQuery) -> SearchPage:
        candidate = normalize_candidate_meta(
            site_id="fake", torrent_id="42", display_name=query.query_text or "synthetic"
        )
        return SearchPage("fake", query.page, (candidate,), False, 1)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        return TorrentDetails(
            normalize_candidate_meta(
                site_id="fake", torrent_id=torrent_id, display_name="synthetic"
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        return TorrentPayload("fake", torrent_id, _TORRENT_BYTES, datetime.now(UTC))


_CANDIDATE_NEXUS_KINDS = (
    SiteKind.HDHOME,
    SiteKind.KEEPFRDS,
    SiteKind.UBITS,
    SiteKind.HDFANS,
    SiteKind.BTSCHOOL,
    SiteKind.PTTIME,
    SiteKind.LINGYIN_CLUB,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CANDIDATE_NEXUS_KINDS)
async def test_candidate_nexus_factory_uses_only_reviewed_origin_and_cookie(
    kind: SiteKind,
) -> None:
    origin = site_profile(kind).base_url
    host = httpx2.URL(origin).host
    cookie = "synthetic-cookie-not-a-passkey"
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        assert request.url.host == host
        assert request.headers.get("cookie") == cookie
        assert request.headers.get("x-api-key") is None
        assert request.url.path == "/index.php"
        return httpx2.Response(200, text='<a href="usercp.php">账号设置</a>')

    factory = SiteAdapterFactory(transport=httpx2.MockTransport(handler))
    adapter = factory.create(
        kind=kind,
        base_url=origin,
        credential_kind=SiteCredentialKind.COOKIE,
        credential=cookie,
    )
    result = await adapter.test_connection()
    assert result.site_id == kind.value.lower()
    assert seen == ["/index.php"]
    assert (await adapter.capabilities()).min_request_interval_seconds == 2.0

    # Reject a direct factory caller overriding the trusted target or
    # treating a Cookie as an API key while these profiles remain pending.
    with pytest.raises(ValueError, match="固定地址"):
        factory.create(
            kind=kind,
            base_url="https://outside.example",
            credential_kind=SiteCredentialKind.COOKIE,
            credential=cookie,
        )
    with pytest.raises(ValueError, match="凭证类型不匹配"):
        factory.create(
            kind=kind,
            base_url=origin,
            credential_kind=SiteCredentialKind.API_KEY,
            credential=cookie,
        )
    assert seen == ["/index.php"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CANDIDATE_NEXUS_KINDS)
async def test_candidate_nexus_site_specific_search_columns_are_not_misclassified(
    kind: SiteKind,
) -> None:
    # Each synthetic row mirrors the observed column counts and offsets, not
    # any real site's search-result body, torrent name, ID or private URL.
    columns = {
        SiteKind.HDHOME: (10, 7, 6, 5, 4),
        SiteKind.UBITS: (10, 7, 6, 5, 4),
        SiteKind.PTTIME: (12, 8, 7, 6, 5),
    }
    count, date_offset, size_offset, seeders_offset, leechers_offset = columns.get(
        kind, (9, 6, 5, 4, 3)
    )
    cells = ["placeholder"] * count
    cells[0] = '<a href="details.php?id=123" title="Synthetic Movie 2026">Synthetic Movie</a>'
    cells[-date_offset] = "2026-09-09 12:00:00"
    cells[-size_offset] = "4.00 GiB"
    cells[-seeders_offset] = "8"
    cells[-leechers_offset] = "2"
    html = "<table><tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr></table>"
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        assert request.url.host == httpx2.URL(site_profile(kind).base_url).host
        assert request.headers.get("cookie") == "synthetic-cookie"
        assert request.url.path == "/torrents.php"
        return httpx2.Response(200, text=html)

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=kind,
        base_url=site_profile(kind).base_url,
        credential_kind=SiteCredentialKind.COOKIE,
        credential="synthetic-cookie",
    )
    page = await adapter.search(SearchQuery(("Synthetic",), SearchMediaType.MOVIE))
    assert paths == ["/torrents.php"]
    assert len(page.items) == 1
    candidate = page.items[0]
    assert candidate.total_size == 4 * 1024**3
    assert candidate.seeders == 8
    assert candidate.leechers == 2
    assert candidate.published_at is not None
    assert candidate.published_at.isoformat() == "2026-09-09T04:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CANDIDATE_NEXUS_KINDS)
async def test_candidate_nexus_remote_page_size_mismatch_preserves_all_candidates(
    kind: SiteKind,
) -> None:
    # page_size is a local hint: a NexusPHP site may return a larger fixed
    # remote page. Truncating results before advancing the remote page would
    # permanently lose candidates.
    origin = site_profile(kind).base_url
    observed_pages: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == httpx2.URL(origin).host
        assert request.headers.get("cookie") == "synthetic-cookie"
        assert request.url.path == "/torrents.php"
        remote_page = request.url.params["page"]
        observed_pages.append(remote_page)
        numbers = range(100, 125) if remote_page == "0" else range(125, 127)
        rows = "".join(
            (
                '<tr><td><a href="details.php?id='
                f'{number}" title="Synthetic Movie {number}">Synthetic</a></td>'
                + "<td>placeholder</td>" * 11
                + "</tr>"
            )
            for number in numbers
        )
        valid_next = (
            '<a href="/torrents.php?search=Synthetic&amp;page=1">next</a>'
            if remote_page == "0"
            else ""
        )
        return httpx2.Response(
            200,
            text=(
                "<table>"
                + rows
                + "</table>"
                + valid_next
                + '<a href="/comments.php?page=1">unrelated pagination</a>'
                + '<a href="https://outside.invalid/torrents.php?page=1">external</a>'
            ),
        )

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=kind,
        base_url=origin,
        credential_kind=SiteCredentialKind.COOKIE,
        credential="synthetic-cookie",
    )
    first = await adapter.search(SearchQuery(("Synthetic",), SearchMediaType.MOVIE, page_size=20))
    second = await adapter.search(
        SearchQuery(("Synthetic",), SearchMediaType.MOVIE, page=2, page_size=20)
    )
    assert len(first.items) == 25
    assert first.has_more
    assert len(second.items) == 2
    assert not second.has_more
    assert len({item.identity for item in (*first.items, *second.items)}) == 27
    assert observed_pages == ["0", "1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CANDIDATE_NEXUS_KINDS)
async def test_candidate_nexus_torrent_link_uses_same_origin_without_separate_passkey(
    kind: SiteKind,
) -> None:
    # Synthetic only. PTTime's observed detail link contains an account-specific
    # passkey parameter, but the link is supplied by the authenticated site
    # itself; do not require a separate credential or log the private URL.
    origin = site_profile(kind).base_url
    secret = "synthetic-download-secret"
    suffix = f"&passkey={secret}" if kind is SiteKind.PTTIME else ""
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        assert request.url.host == httpx2.URL(origin).host
        assert request.headers.get("cookie") == "synthetic-cookie"
        if request.url.path == "/details.php":
            assert request.url.params["id"] == "123"
            return httpx2.Response(
                200,
                text=f'<a href="/download.php?id=123{suffix}">Synthetic download</a>',
            )
        assert request.url.path == "/download.php"
        assert request.url.params["id"] == "123"
        if kind is SiteKind.PTTIME:
            assert request.url.params["passkey"] == secret
        else:
            assert "passkey" not in request.url.params
        return httpx2.Response(200, content=_VALID_TORRENT_BYTES)

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=kind,
        base_url=origin,
        credential_kind=SiteCredentialKind.COOKIE,
        credential="synthetic-cookie",
    )
    result = await adapter.fetch_torrent("123")
    assert result.content == _VALID_TORRENT_BYTES
    assert requested == ["/details.php", "/download.php"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CANDIDATE_NEXUS_KINDS)
@pytest.mark.parametrize(
    "invalid_content",
    (
        b"d3:foo3:bare",
        b"d4:infod4:name9:syntheticee",
        b"<html>login required</html>",
    ),
)
async def test_candidate_nexus_invalid_metainfo_fails_closed_without_content_leaks(
    kind: SiteKind, invalid_content: bytes
) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        assert request.url.host == httpx2.URL(site_profile(kind).base_url).host
        assert request.headers.get("cookie") == "synthetic-cookie"
        if request.url.path == "/details.php":
            return httpx2.Response(200, text='<a href="/download.php?id=123">Download</a>')
        assert request.url.path == "/download.php"
        return httpx2.Response(200, content=invalid_content)

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=kind,
        base_url=site_profile(kind).base_url,
        credential_kind=SiteCredentialKind.COOKIE,
        credential="synthetic-cookie",
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.fetch_torrent("123")
    assert exc.value.code == "SITE_INVALID_RESPONSE"
    assert "synthetic-cookie" not in str(exc.value)
    assert "login required" not in str(exc.value)
    assert requested == ["/details.php", "/download.php"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CANDIDATE_NEXUS_KINDS)
async def test_candidate_nexus_torrent_link_cannot_forward_cookie_or_passkey_off_origin(
    kind: SiteKind,
) -> None:
    # The HTTP client must reject a cross-origin download href before any
    # network request even if it carries a synthetic passkey-like parameter.
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(request.url.path)
        assert request.url.host == httpx2.URL(site_profile(kind).base_url).host
        assert request.url.path == "/details.php"
        return httpx2.Response(
            200,
            text=(
                '<a href="https://outside.invalid/download.php?id=123'
                '&passkey=synthetic-private-token">External download</a>'
            ),
        )

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=kind,
        base_url=site_profile(kind).base_url,
        credential_kind=SiteCredentialKind.COOKIE,
        credential="synthetic-cookie",
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.fetch_torrent("123")
    assert exc.value.code == "SITE_DOWNLOAD_URL_INVALID"
    assert "synthetic-private-token" not in str(exc.value)
    assert requested == ["/details.php"]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("torrents_posted", 1.5),
        ("seeding_count", 2.25),
        ("seeding_size_bytes", 1024.5),
        ("uploaded_bytes", float("nan")),
        ("downloaded_bytes", float("inf")),
        ("real_uploaded_bytes", True),
        ("real_downloaded_bytes", -1),
    ],
)
def test_site_user_profile_shared_integer_statistics_reject_invalid_types(
    field: str, invalid: object
) -> None:
    with pytest.raises(ValueError, match="必须是非负整数"):
        SiteUserProfile("synthetic", **{field: invalid})  # type: ignore[arg-type]


def test_site_user_profile_shared_integer_statistics_accept_zero_and_large_int() -> None:
    value = SiteUserProfile(
        "synthetic", torrents_posted=0, seeding_count=3, seeding_size_bytes=2**58
    )
    assert value.torrents_posted == 0
    assert value.seeding_size_bytes == 2**58


@pytest.mark.asyncio
async def test_fake_site_adapter_satisfies_shared_read_only_contract() -> None:
    await assert_read_only_site_adapter_contract(
        SiteAdapterContractCase(
            FakeSiteAdapter(),
            SearchQuery(("synthetic",), SearchMediaType.MOVIE),
            "fake",
            "42",
        )
    )


@pytest.mark.asyncio
async def test_mteam_adapter_satisfies_shared_contract_and_never_forwards_api_key() -> None:
    api_key = "mteam_synthetic_api_key"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.host == "download.m-team.cc":
            assert request.headers.get("x-api-key") is None
            return httpx2.Response(200, content=_TORRENT_BYTES)
        assert request.url.host == "api.m-team.cc"
        assert request.headers.get("x-api-key") == api_key
        assert request.headers.get("origin") == "https://kp.m-team.cc"
        if request.url.path == "/api/member/profile":
            return httpx2.Response(200, json={"code": "0", "data": {"id": "1"}})
        if request.url.path == "/api/torrent/search":
            payload = json.loads(request.content)
            assert payload["mode"] == "movie"
            assert payload["keyword"] == "synthetic"
            return httpx2.Response(
                200,
                json={"code": "0", "data": {"data": [_mteam_row()], "total": "1"}},
            )
        if request.url.path == "/api/torrent/detail":
            assert b"id=123" in request.content
            return httpx2.Response(200, json={"code": "0", "data": _mteam_row()})
        if request.url.path == "/api/torrent/genDlToken":
            assert b"id=123" in request.content
            return httpx2.Response(
                200,
                json={
                    "code": "0",
                    "data": "https://download.m-team.cc/synthetic.torrent?token=opaque",
                },
            )
        return httpx2.Response(404)

    adapter = MTeamAdapter(api_key, transport=httpx2.MockTransport(handler))
    await assert_read_only_site_adapter_contract(
        SiteAdapterContractCase(
            adapter,
            SearchQuery(("synthetic",), SearchMediaType.MOVIE),
            "mteam",
            "123",
        )
    )

    search = next(request for request in requests if request.url.path == "/api/torrent/search")
    assert search.url.query == b""
    assert (await adapter.capabilities()).min_request_interval_seconds == 90.0


@pytest.mark.asyncio
async def test_mteam_configured_site_origin_is_not_used_as_api_origin() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"code": "0", "data": {"id": "1"}})

    adapter = MTeamAdapter(
        "synthetic",
        base_url="https://kp.m-team.cc",
        transport=httpx2.MockTransport(handler),
    )

    await adapter.test_connection()

    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "api.m-team.cc"
    assert request.headers["origin"] == "https://kp.m-team.cc"


@pytest.mark.asyncio
async def test_mteam_user_profile_normalizes_nested_stats_and_preserves_missing_values() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "id": "42",
                    "username": "SyntheticUser",
                    "userClass": "Elite",
                    "stats": {
                        "uploaded": "1099511627776",
                        "downloaded": "549755813888",
                        "realUploaded": "1073741824",
                        "torrentCount": "12",
                        "seedingCount": "34",
                        "bonus": "56.75",
                    },
                },
            },
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()

    assert profile.site_id == "mteam"
    assert profile.uid == "42"
    assert profile.username == "SyntheticUser"
    assert profile.user_level == "Elite"
    assert profile.uploaded_bytes == 1099511627776
    assert profile.downloaded_bytes == 549755813888
    assert profile.real_uploaded_bytes == 1073741824
    assert profile.real_downloaded_bytes is None
    assert profile.ratio == 2.0
    assert profile.torrents_posted == 12
    assert profile.seeding_count == 34
    assert profile.bonus == 56.75
    assert profile.bonus_per_hour is None


@pytest.mark.asyncio
async def test_mteam_user_profile_supports_current_member_count_shape() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "id": "42",
                    "username": "SyntheticUser",
                    "role": "USER",
                    "memberCount": {
                        "uploaded": "1099511627776",
                        "downloaded": "549755813888",
                        "shareRate": "2.0",
                        "bonus": "56.75",
                    },
                },
            },
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()

    assert profile.site_id == "mteam"
    assert profile.uid == "42"
    assert profile.username == "SyntheticUser"
    assert profile.user_level is None  # 身份角色不是站点用户等级
    assert profile.uploaded_bytes == 1099511627776
    assert profile.downloaded_bytes == 549755813888
    assert profile.ratio == 2.0
    assert profile.bonus == 56.75


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_count", "bad_size"),
    [(1.75, 2.5), (-1, -2), (True, False), ("1.5", "2.5")],
)
async def test_mteam_profile_does_not_truncate_invalid_integer_statistics(
    bad_count: object, bad_size: object
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "id": "42",
                    "memberStatus": {"vip": True, "role": "USER", "level": 7},
                    "memberCount": {
                        "uploaded": "1099511627776",
                        "bonus": "56.75",
                        "seedingCount": bad_count,
                        "seedingSizeBytes": bad_size,
                    },
                },
            },
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level is None  # VIP/role/level ID cannot prove a named grade.
    assert profile.uploaded_bytes == 1099511627776
    assert profile.bonus == 56.75
    assert profile.seeding_count is None
    assert profile.seeding_size_bytes is None


@pytest.mark.asyncio
async def test_mteam_profile_uses_named_grade_and_nested_member_statistics() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "level": "7",
                    "userClassName": "Elite",
                    "memberCount": {
                        "stats": {
                            "seedingCount": 24,
                            "torrentCount": 5,
                            "seedingSizeBytes": 4294967296,
                            "seedingPoints": 12.5,
                            "bonusPerHour": 1.25,
                        }
                    },
                },
            },
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "Elite"
    assert profile.seeding_count == 24
    assert profile.torrents_posted == 5
    assert profile.seeding_size_bytes == 4294967296
    assert profile.seeding_points == 12.5
    assert profile.bonus_per_hour == 1.25


@pytest.mark.asyncio
async def test_mteam_numeric_grade_without_official_name_is_unknown() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200, json={"code": "0", "data": {"level": 7, "userClass": "7", "role": "USER"}}
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level is None


@pytest.mark.asyncio
async def test_mteam_numeric_outer_grade_does_not_mask_nested_official_name() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "userClassName": "7",
                    "profile": {"userClassName": "Elite Member"},
                },
            },
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "Elite Member"


@pytest.mark.asyncio
async def test_mteam_profile_reads_bounded_nested_profile_envelopes() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/member/profile"
        return httpx2.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "member": {
                        "user": {
                            "profile": {
                                "userLevelName": "Elite",
                                "seedingCount": 23,
                                "seedingSizeBytes": 4294967296,
                            }
                        }
                    },
                    "untrustedSearchResults": [{"seedingCount": 9999}],
                },
            },
        )

    profile = await MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler)
    ).fetch_user_profile()
    assert profile.user_level == "Elite"
    assert profile.seeding_count == 23
    assert profile.seeding_size_bytes == 4294967296
    assert profile.torrents_posted is None


@pytest.mark.asyncio
async def test_mteam_external_id_search_uses_explicit_api_fields() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"code": "0", "data": {"data": [], "total": "0"}})

    adapter = MTeamAdapter("synthetic", transport=httpx2.MockTransport(handler))
    from backend.app.domain.media_matching import ExternalMediaId

    await adapter.search(
        SearchQuery(
            ("movie",),
            SearchMediaType.MOVIE,
            external_ids=(
                ExternalMediaId("imdb", "tt1234567"),
                ExternalMediaId("douban", "1295644"),
            ),
        )
    )

    assert bodies == [
        {
            "pageNumber": 1,
            "pageSize": 50,
            "mode": "movie",
            "keyword": "movie",
            "imdb": "https://www.imdb.com/title/tt1234567/",
            "douban": "https://movie.douban.com/subject/1295644/",
        }
    ]


@pytest.mark.asyncio
async def test_mteam_rejects_download_url_outside_configured_site_family() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_hosts.append(request.url.host or "")
        return httpx2.Response(
            200, json={"code": "0", "data": "https://attacker.invalid/payload.torrent"}
        )

    adapter = MTeamAdapter("synthetic", transport=httpx2.MockTransport(handler))

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")

    assert failure.value.code == "SITE_DOWNLOAD_URL_INVALID"
    assert requested_hosts == ["api.m-team.cc"]
    assert "attacker.invalid" not in str(failure.value)


@pytest.mark.asyncio
async def test_mteam_follows_allowlisted_download_redirect_without_forwarding_api_key() -> None:
    api_key = "mteam_synthetic_api_key"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/api/torrent/genDlToken":
            assert request.headers.get("x-api-key") == api_key
            return httpx2.Response(
                200,
                json={
                    "code": "0",
                    "data": "https://api.m-team.cc/api/rss/dlv2?tid=123&sign=opaque",
                },
            )
        assert request.headers.get("x-api-key") is None
        if request.url.host == "api.m-team.cc":
            return httpx2.Response(
                302,
                headers={
                    "location": "https://fr1.halomt.com/?app_id=1&sign=opaque",
                },
            )
        assert request.url.host == "fr1.halomt.com"
        return httpx2.Response(200, content=_TORRENT_BYTES)

    adapter = MTeamAdapter(api_key, transport=httpx2.MockTransport(handler))

    payload = await adapter.fetch_torrent("123")

    assert payload.content == _TORRENT_BYTES
    assert [request.url.host for request in requests] == [
        "api.m-team.cc",
        "api.m-team.cc",
        "fr1.halomt.com",
    ]


@pytest.mark.asyncio
async def test_mteam_rejects_download_redirect_to_untrusted_host() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_hosts.append(request.url.host or "")
        if request.url.path == "/api/torrent/genDlToken":
            return httpx2.Response(
                200,
                json={"code": "0", "data": "https://api.m-team.cc/api/rss/dlv2?tid=123"},
            )
        return httpx2.Response(302, headers={"location": "https://attacker.invalid/file"})

    adapter = MTeamAdapter("synthetic", transport=httpx2.MockTransport(handler))

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")

    assert failure.value.code == "SITE_DOWNLOAD_URL_INVALID"
    assert requested_hosts == ["api.m-team.cc", "api.m-team.cc"]


@pytest.mark.asyncio
async def test_mteam_rejects_excessive_download_redirects() -> None:
    redirect_requests = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal redirect_requests
        if request.url.path == "/api/torrent/genDlToken":
            return httpx2.Response(
                200,
                json={"code": "0", "data": "https://api.m-team.cc/api/rss/dlv2?tid=123"},
            )
        redirect_requests += 1
        return httpx2.Response(
            302,
            headers={"location": f"https://fr{redirect_requests}.halomt.com/?sign=opaque"},
        )

    adapter = MTeamAdapter("synthetic", transport=httpx2.MockTransport(handler))

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")

    assert failure.value.code == "SITE_TORRENT_FETCH_FAILED"
    assert redirect_requests == 4


@pytest.mark.asyncio
async def test_mteam_enforces_streaming_torrent_size_limit() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/torrent/genDlToken":
            return httpx2.Response(
                200,
                json={"code": "0", "data": "https://download.m-team.cc/oversize.torrent"},
            )
        return httpx2.Response(200, content=b"123456789")

    adapter = MTeamAdapter(
        "synthetic", transport=httpx2.MockTransport(handler), max_torrent_bytes=8
    )

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("123")

    assert failure.value.code == "SITE_TORRENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_mteam_errors_do_not_echo_api_key_or_remote_message() -> None:
    api_key = "PACKBREAKER-MTEAM-CANARY-41e2"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"code": "403", "message": f"bad key {api_key}", "data": None},
        )

    adapter = MTeamAdapter(api_key, transport=httpx2.MockTransport(handler))

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.search(SearchQuery(("movie",), SearchMediaType.MOVIE))

    assert failure.value.code == "SITE_AUTH_FAILED"
    assert api_key not in str(failure.value)


def _mteam_row() -> dict[str, object]:
    return {
        "id": "123",
        "name": "Synthetic.Movie.2026.2160p.WEB-DL.HEVC",
        "size": "4096",
        "createdDate": "2026-09-09T12:00:00+08:00",
        "category": "401",
        "imdb": "https://www.imdb.com/title/tt1234567/",
        "douban": "https://movie.douban.com/subject/1295644/",
        "status": {"seeders": "8", "leechers": "2"},
    }
