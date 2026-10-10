from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from backend.app.domain.media_matching import ExternalMediaId
from backend.app.domain.site_config import (
    SiteCredentialKind,
    SiteKind,
    SiteSupportStatus,
    site_profile,
)
from backend.app.domain.site_search import SearchMediaType, SearchQuery
from backend.app.infrastructure.adapters.rousi_pro import RousiProCandidateAdapter
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory

_ORIGIN = "https://rousi.pro"
_KEY = "synthetic-api-key-private"
_COOKIE = "synthetic-cookie-private"
_VALID_TORRENT = (
    b"d4:infod6:lengthi1e4:name9:synthetic12:piece lengthi16384e6:pieces20:01234567890123456789ee"
)


@pytest.mark.asyncio
async def test_rousi_pro_candidate_uses_api_key_header_only_on_reviewed_readonly_path() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == f"{_ORIGIN}/api/points/attendance/stats"
        assert request.headers.get("api-token") == _KEY
        assert request.headers.get("cookie") is None
        assert request.headers.get("authorization") is None
        assert request.url.query == b""
        return httpx2.Response(200, json={"code": 0, "data": {"synthetic": True}})

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    assert site_profile(SiteKind.ROUSI_PRO).support_status is SiteSupportStatus.SUPPORTED
    assert (await adapter.test_connection()).site_id == "rousi_pro"
    assert len(requests) == 1

    # The search endpoint has an independently verified contract. Torrent
    # details/download and the removed user profile still fail closed.
    for action in (
        adapter.fetch_details("123"),
        adapter.fetch_torrent("123"),
        adapter.fetch_user_profile(),
    ):
        with pytest.raises(SiteAdapterError) as exc:
            await action
        assert exc.value.code == "SITE_ADAPTER_PENDING"
        assert _KEY not in str(exc.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_id",
    (
        "123",
        "../download",
        "https://outside.invalid/collect",
        "123?passkey=synthetic-private-value",
    ),
)
async def test_rousi_pro_unverified_details_and_download_fail_closed_without_network(
    candidate_id: str,
) -> None:
    attempted: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempted.append(request.url.path)
        raise AssertionError("Unverified torrent details/download must not access any host")

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    for operation in (adapter.fetch_details, adapter.fetch_torrent):
        with pytest.raises(SiteAdapterError) as exc:
            await operation(candidate_id)
        assert exc.value.code == "SITE_ADAPTER_PENDING"
        assert _KEY not in str(exc.value)
        assert "synthetic-private-value" not in str(exc.value)
    assert attempted == []


@pytest.mark.asyncio
async def test_rousi_cookie_download_separates_headers_and_validates_metainfo() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == f"{_ORIGIN}/api/v1/torrents/123/download"
        assert request.headers.get("cookie") == _COOKIE
        assert request.headers.get("api-token") is None
        assert request.headers.get("authorization") is None
        return httpx2.Response(
            200,
            content=_VALID_TORRENT,
            headers={"content-type": "application/x-bittorrent"},
        )

    adapter = RousiProCandidateAdapter(
        _KEY,
        download_cookie=_COOKIE,
        transport=httpx2.MockTransport(handler),
    )
    result = await adapter.fetch_torrent("123")
    assert result.site_id == "rousi_pro"
    assert result.torrent_id == "123"
    assert result.content == _VALID_TORRENT
    assert len(requests) == 1
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.fetch_details("123")
    assert exc.value.code == "SITE_ADAPTER_PENDING"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "torrent_id",
    (
        "",
        "0",
        "001",
        "-1",
        "12/3",
        "../123",
        "123?passkey=synthetic-private",
        "https://outside.invalid/collect",
        "1" * 19,
    ),
)
async def test_rousi_pro_explicit_cookie_rejects_untrusted_ids_before_network(
    torrent_id: str,
) -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        raise AssertionError("Invalid remote torrent ID must never reach the network")

    adapter = RousiProCandidateAdapter(
        _KEY,
        download_cookie=_COOKIE,
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.fetch_torrent(torrent_id)
    assert exc.value.code == "SITE_INVALID_TORRENT_ID"
    assert "synthetic-private" not in str(exc.value)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "body", "extra_headers", "error_code"),
    (
        (401, "text/plain", b"private-cookie", {}, "SITE_AUTH_FAILED"),
        (403, "text/plain", b"private-cookie", {}, "SITE_AUTH_FAILED"),
        (429, "text/plain", b"private-cookie", {}, "SITE_RATE_LIMITED"),
        (500, "application/problem+json", b'{"error":"temporary"}', {}, "SITE_UNAVAILABLE"),
        (503, "text/plain", b"temporary maintenance", {}, "SITE_UNAVAILABLE"),
        (
            302,
            "text/plain",
            b"private-cookie",
            {"location": "https://outside.invalid/steal"},
            "SITE_HTTP_ERROR",
        ),
        (200, "text/html", b"<html>private-cookie</html>", {}, "SITE_INVALID_RESPONSE"),
        (200, "application/x-bittorrent", b"d3:foo3:bare", {}, "SITE_INVALID_RESPONSE"),
        (
            200,
            "application/x-bittorrent",
            _VALID_TORRENT,
            {"content-length": str(20 * 1024 * 1024 + 1)},
            "SITE_RESPONSE_TOO_LARGE",
        ),
    ),
)
async def test_rousi_pro_cookie_download_fails_closed_without_secrets(
    status: int,
    content_type: str,
    body: bytes,
    extra_headers: dict[str, str],
    error_code: str,
) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        assert request.headers.get("cookie") == _COOKIE
        assert request.headers.get("api-token") is None
        return httpx2.Response(
            status,
            content=body,
            headers={"content-type": content_type, **extra_headers},
        )

    adapter = RousiProCandidateAdapter(
        _KEY,
        download_cookie=_COOKIE,
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.fetch_torrent("123")
    assert exc.value.code == error_code
    assert _KEY not in str(exc.value)
    assert _COOKIE not in str(exc.value)
    assert "outside.invalid" not in str(exc.value)
    assert requested == [f"{_ORIGIN}/api/v1/torrents/123/download"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    (
        (429, "SITE_RATE_LIMITED", True),
        (500, "SITE_UNAVAILABLE", True),
        (503, "SITE_UNAVAILABLE", True),
        (401, "SITE_AUTH_FAILED", False),
        (403, "SITE_AUTH_FAILED", False),
        (400, "SITE_HTTP_ERROR", False),
        (404, "SITE_HTTP_ERROR", False),
        (302, "SITE_HTTP_ERROR", False),
    ),
)
@pytest.mark.parametrize("operation", ("search", "test_connection"))
async def test_rousi_readonly_http_error_retryability(
    operation: str, status: int, expected_code: str, retryable: bool
) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if operation == "search":
            assert request.url.path == "/api/v1/torrents"
            assert request.headers.get("api-token") is None
        else:
            assert request.url.path == "/api/points/attendance/stats"
            assert request.headers.get("api-token") == _KEY
        assert request.headers.get("cookie") is None
        return httpx2.Response(status, content=b"synthetic-secret-body")

    adapter = RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        if operation == "search":
            await adapter.search(SearchQuery(("synthetic",), SearchMediaType.MOVIE))
        else:
            await adapter.test_connection()
    assert failure.value.code == expected_code
    assert failure.value.retryable is retryable
    assert "synthetic-secret-body" not in str(failure.value)
    assert _KEY not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("search", "test_connection"))
async def test_rousi_readonly_connect_error_retryable(operation: str) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ConnectError("synthetic external private connection detail")

    adapter = RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        if operation == "search":
            await adapter.search(SearchQuery(("synthetic",), SearchMediaType.MOVIE))
        else:
            await adapter.test_connection()
    assert failure.value.code == "SITE_UNAVAILABLE"
    assert failure.value.retryable is True
    assert "synthetic external private" not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("search", "test_connection"))
@pytest.mark.parametrize("date_header", (False, True))
async def test_rousi_readonly_429_preserves_retry_after(operation: str, date_header: bool) -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    header = (
        format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
        if date_header
        else "120"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers.get("cookie") is None
        return httpx2.Response(429, headers={"Retry-After": header})

    adapter = RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        if operation == "search":
            await adapter.search(SearchQuery(("synthetic",), SearchMediaType.MOVIE))
        else:
            await adapter.test_connection()
    assert failure.value.code == "SITE_RATE_LIMITED"
    assert failure.value.retryable is True
    assert failure.value.retry_after_seconds is not None
    assert 115 <= failure.value.retry_after_seconds <= 121


@pytest.mark.asyncio
async def test_rousi_readonly_429_invalid_retry_after_fails_closed() -> None:
    from math import isfinite

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "NaN"})

    adapter = RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        await adapter.search(SearchQuery(("synthetic",), SearchMediaType.MOVIE))
    assert failure.value.retry_after_seconds is not None
    assert not isfinite(failure.value.retry_after_seconds)


@pytest.mark.asyncio
async def test_rousi_rate_limit_does_not_burst_and_torrent_not_replayed() -> None:
    from backend.app.infrastructure.site_reliability import (
        SiteReliabilityPolicy,
        SiteReliabilityRegistry,
    )

    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if request.url.path == "/api/v1/torrents":
            assert request.headers.get("api-token") is None
            return httpx2.Response(429, headers={"Retry-After": "120"})
        assert request.url.path == "/api/v1/torrents/123/download"
        assert request.headers.get("cookie") == _COOKIE
        return httpx2.Response(503, content=b"synthetic-private-error")

    registry = SiteReliabilityRegistry(
        SiteReliabilityPolicy(max_attempts=3, retry_deadline_seconds=5.0)
    )
    search_adapter = registry.wrap(
        config_id="rousi-search",
        config_version=1,
        adapter=RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(SiteAdapterError) as limited:
        await search_adapter.search(SearchQuery(("synthetic",), SearchMediaType.MOVIE))
    assert limited.value.code == "SITE_RATE_LIMITED"
    assert limited.value.retry_after_seconds == 120
    assert len(calls) == 1

    download_adapter = registry.wrap(
        config_id="rousi-download",
        config_version=1,
        adapter=RousiProCandidateAdapter(
            _KEY, download_cookie=_COOKIE, transport=httpx2.MockTransport(handler)
        ),
    )
    with pytest.raises(SiteAdapterError) as unavailable:
        await download_adapter.fetch_torrent("123")
    assert unavailable.value.code == "SITE_UNAVAILABLE"
    assert len(calls) == 2
    assert "synthetic-private-error" not in str(unavailable.value)


@pytest.mark.asyncio
async def test_rousi_pro_candidate_search_uses_public_schema_without_credentials() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/torrents"
        assert request.headers.get("api-token") is None
        assert request.headers.get("cookie") is None
        assert request.headers.get("authorization") is None
        assert request.url.params["query"] == "synthetic"
        assert request.url.params["offset"] == "10"
        assert request.url.params["limit"] == "10"
        assert "page" not in request.url.params
        assert "api_token" not in request.url.params
        return httpx2.Response(
            200,
            json={
                "limit": 10,
                "offset": 10,
                "total": 301,
                "items": [
                    {
                        "id": 123,
                        "name": "Synthetic.Movie.2026.1080p.WEB-DL",
                        "size_bytes": 4294967296,
                        "seeders": 8,
                        "leechers": 2,
                        "category": {"id": 1, "name": "movie"},
                        "uploaded_at": "2026-09-09T12:00:00+08:00",
                    },
                    {
                        "id": 124,
                        "name": "Synthetic.Movie.2026.2160p.WEB-DL",
                        "size_bytes": 8589934592,
                        "seeders": 3,
                        "leechers": 1,
                        "category": "movie",
                        "uploaded_at": "2026-09-09T04:00:00Z",
                    },
                ],
            },
        )

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    assert (await adapter.capabilities()).supports_pagination
    page = await adapter.search(
        SearchQuery(("Synthetic",), SearchMediaType.MOVIE, page=2, page_size=10)
    )
    assert page.page == 2
    assert page.has_more
    assert page.total_hint == 301
    assert len(page.items) == 2
    assert page.items[0].torrent_id == "123"
    assert page.items[0].total_size == 4294967296
    assert page.items[0].seeders == 8
    assert page.items[0].leechers == 2
    assert page.items[0].category == "movie"
    assert page.items[0].published_at is not None
    assert page.items[0].published_at.isoformat() == "2026-09-09T04:00:00+00:00"
    assert page.items[1].published_at == page.items[0].published_at
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rousi_query_uses_real_offset_and_accepts_offset_only_metadata() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/v1/torrents"
        assert request.url.params["query"] == "九品芝麻官"
        assert request.url.params["offset"] == "0"
        assert request.url.params["limit"] == "20"
        assert "page" not in request.url.params
        assert request.headers.get("api-token") is None
        assert request.headers.get("cookie") is None
        return httpx2.Response(
            200,
            json={
                "limit": 20,
                "offset": 0,
                "total": 4,
                "items": [
                    {
                        "id": i + 101,
                        "name": f"九品芝麻官.1994.{i}.1080p.BluRay",
                        "size_bytes": 4_000_000_000,
                    }
                    for i in range(4)
                ],
            },
        )

    adapter = RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler))
    result = await adapter.search(
        SearchQuery(("九品芝麻官",), SearchMediaType.MOVIE, page=1, page_size=20)
    )
    assert len(result.items) == 4
    assert result.total_hint == 4
    assert result.has_more is False


@pytest.mark.asyncio
async def test_rousi_search_rejects_legacy_unfiltered_auth_envelope() -> None:
    """The token-backed 8995-result response must never be treated as a match."""
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.url.path == "/api/v1/torrents"
        assert request.url.params["query"] == "九品芝麻官"
        assert request.headers.get("api-token") is None
        assert request.headers.get("cookie") is None
        assert request.headers.get("authorization") is None
        return httpx2.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "page": 1,
                    "page_size": 100,
                    "total": 8995,
                    "torrents": [{"id": 123, "title": "Unrelated.Movie.2014", "size": 1234}],
                },
            },
        )

    adapter = RousiProCandidateAdapter(_KEY, transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.search(
            SearchQuery(("九品芝麻官",), SearchMediaType.MOVIE, page=1, page_size=20)
        )
    assert exc.value.code == "SITE_INVALID_RESPONSE"
    assert _KEY not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(offset=50),
        lambda data: data.update(limit=0),
        lambda data: data.update(limit=100),
        lambda data: data.update(limit=1, items=[*data["items"], {**data["items"][0], "id": 124}]),
        lambda data: data.update(total=-1),
        lambda data: data.update(items=[], total=101),
        lambda data: data.update(items="not-a-list"),
        lambda data: data["items"][0].update(id=True),
        lambda data: data["items"][0].update(name=""),
        lambda data: data["items"][0].update(size_bytes=-1),
        lambda data: data["items"].append(data["items"][0].copy()),
    ],
)
async def test_rousi_pro_candidate_search_rejects_invalid_schema_without_echo(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    data: dict[str, Any] = {
        "limit": 50,
        "offset": 0,
        "total": 101,
        "items": [
            {
                "id": 123,
                "name": "Synthetic.Movie.2026",
                "size_bytes": 42,
                "seeders": 8,
                "leechers": 2,
                "uploaded_at": "2026-09-09T04:00:00Z",
            }
        ],
    }
    mutate(data)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/v1/torrents"
        assert request.headers.get("api-token") is None
        return httpx2.Response(200, json=data)

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.search(SearchQuery(("Synthetic",), SearchMediaType.MOVIE))
    assert exc.value.code == "SITE_INVALID_RESPONSE"
    assert _KEY not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incomplete_field",
    ({"size_bytes": None}, {"size_bytes": 0}, {"id": 10**18}),
)
async def test_rousi_isolated_analysis_refuses_incomplete_search_candidate(
    incomplete_field: dict[str, object],
) -> None:
    requests: list[str] = []
    candidate: dict[str, object] = {
        "id": 123,
        "name": "Synthetic.Movie.2026",
        "size_bytes": 16,
        "seeders": 3,
        "leechers": 0,
    }
    candidate.update(incomplete_field)

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        assert request.url.path == "/api/v1/torrents"
        assert request.headers.get("api-token") is None
        assert request.headers.get("cookie") is None
        return httpx2.Response(
            200,
            json={
                "limit": 50,
                "offset": 0,
                "total": 1,
                "items": [candidate],
            },
        )

    adapter = RousiProCandidateAdapter(
        _KEY,
        download_cookie=_COOKIE,
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.search(SearchQuery(("Synthetic",), SearchMediaType.MOVIE))
    assert exc.value.code == "SITE_INVALID_RESPONSE"
    assert _KEY not in str(exc.value)
    assert _COOKIE not in str(exc.value)
    assert requests == ["/api/v1/torrents"]


@pytest.mark.asyncio
async def test_rousi_pro_candidate_rejects_external_id_only_query_before_network() -> None:
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        raise AssertionError("An unverified external-id-only query must not reach the site")

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.search(
            SearchQuery(
                (),
                SearchMediaType.MOVIE,
                external_ids=(ExternalMediaId("imdb", "tt1234567"),),
            )
        )
    assert exc.value.code == "SITE_UNSUPPORTED_QUERY"
    assert requests == []


def test_rousi_pro_candidate_refuses_cookie_and_unreviewed_origin_without_network() -> None:
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        raise AssertionError("blocked credentials must not be sent")

    factory = SiteAdapterFactory(transport=httpx2.MockTransport(handler))
    with pytest.raises(ValueError, match="凭证类型"):
        factory.create(
            kind=SiteKind.ROUSI_PRO,
            base_url=_ORIGIN,
            credential_kind=SiteCredentialKind.COOKIE,
            credential=_KEY,
        )
    with pytest.raises(ValueError, match="固定地址"):
        factory.create(
            kind=SiteKind.ROUSI_PRO,
            base_url="https://outside.invalid",
            credential_kind=SiteCredentialKind.API_KEY,
            credential=_KEY,
        )
    assert requests == []


@pytest.mark.parametrize(
    ("api_key", "download_cookie"),
    (
        (_KEY + "\rX-Injected: value", _COOKIE),
        (_KEY + "\nX-Injected: value", _COOKIE),
        (_KEY + "\x00", _COOKIE),
        (_KEY, _COOKIE + "\rX-Injected: value"),
        (_KEY, _COOKIE + "\nX-Injected: value"),
        (_KEY, _COOKIE + "\x00"),
    ),
)
def test_rousi_direct_factory_rejects_header_control_characters_before_network(
    api_key: str, download_cookie: str
) -> None:
    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Invalid credential must never reach an HTTP transport")

    with pytest.raises(ValueError) as invalid:
        SiteAdapterFactory(transport=httpx2.MockTransport(forbidden)).create(
            kind=SiteKind.ROUSI_PRO,
            base_url=_ORIGIN,
            credential_kind=SiteCredentialKind.API_KEY,
            credential=api_key,
            download_cookie=download_cookie,
        )
    assert _KEY not in str(invalid.value)
    assert _COOKIE not in str(invalid.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected_code"),
    [
        (401, {"code": 401, "message": _KEY}, "SITE_AUTH_FAILED"),
        (403, {"code": 403, "message": _KEY}, "SITE_AUTH_FAILED"),
        (429, {"code": 429, "message": _KEY}, "SITE_RATE_LIMITED"),
        (302, {"code": 0, "data": {}}, "SITE_HTTP_ERROR"),
        (200, {"code": 401, "message": _KEY}, "SITE_AUTH_FAILED"),
        (200, {"code": True, "data": {}}, "SITE_INVALID_RESPONSE"),
        (200, {"code": 0, "data": _KEY}, "SITE_INVALID_RESPONSE"),
    ],
)
async def test_rousi_pro_rejects_failed_auth_or_malformed_envelopes_without_leaking_key(
    status: int, payload: dict[str, object], expected_code: str
) -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        return httpx2.Response(
            status, json=payload, headers={"location": "https://outside.invalid/leak"}
        )

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.test_connection()
    assert exc.value.code == expected_code
    assert _KEY not in str(exc.value)
    assert paths == ["/api/points/attendance/stats"]


@pytest.mark.asyncio
async def test_rousi_pro_rejects_large_json_without_storing_or_echoing_response() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"code": 0, "data": {"secret": "x" * 40000}})

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=_ORIGIN,
        credential_kind=SiteCredentialKind.API_KEY,
        credential=_KEY,
    )
    with pytest.raises(SiteAdapterError) as exc:
        await adapter.test_connection()
    assert exc.value.code == "SITE_RESPONSE_TOO_LARGE"
    assert _KEY not in str(exc.value)
