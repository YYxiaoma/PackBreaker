from __future__ import annotations

from urllib.parse import parse_qs

import httpx2
import pytest

from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_search import SearchMediaType, SearchQuery, SearchSortHint
from backend.app.infrastructure.adapters.nexusphp import HDTimeAdapter
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
