from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2
import pytest

from backend.app.domain.site_adapter import (
    SiteConnectionResult,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.sites import MTeamAdapter, SiteAdapterError
from tests.contract.site_adapter_contract import (
    SiteAdapterContractCase,
    assert_read_only_site_adapter_contract,
)

_TORRENT_BYTES = b"d4:infod4:name9:syntheticee"


class FakeSiteAdapter:
    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(supports_pagination=True, requires_download_token=False)

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

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
        assert request.headers.get("x-api-key") == api_key
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
