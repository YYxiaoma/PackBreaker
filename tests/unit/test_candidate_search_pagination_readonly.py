from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx2
import pytest

from backend.app.domain.site_config import SiteKind, site_profile
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchPage,
    SearchQuery,
    SearchSortHint,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from scripts import check_candidate_search_pagination_readonly as acceptance


def _config(cookie: str = "synthetic-cookie") -> dict[str, object]:
    return {
        "KeepFrds": {
            "url": site_profile(SiteKind.KEEPFRDS).base_url,
            "auth_type": "cookie",
            "cookie": cookie,
        }
    }


def _page(page: int, ids: tuple[str, ...], has_more: bool) -> SearchPage:
    return SearchPage(
        "keepfrds",
        page,
        tuple(
            normalize_candidate_meta(
                site_id="keepfrds",
                torrent_id=identifier,
                display_name=f"Synthetic.Movie.2024.{identifier}",
                total_size=10,
                published_at=datetime(2024, 1, 1, tzinfo=UTC),
                seeders=1,
                leechers=0,
            )
            for identifier in ids
        ),
        has_more,
    )


def test_pagination_probe_never_networks_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_SECRET_PATH", tmp_path / "missing.secret")
    assert acceptance.main(["--site", "keepfrds"]) == 2
    assert acceptance.main(["--site", "keepfrds", "--live"]) == 2
    assert acceptance.main(["--site", "keepfrds", "--acknowledge-site-search"]) == 2
    assert "未发起站点请求" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_two_page_probe_reports_distinct_ids_without_leaking_identity() -> None:
    class FakeAdapter:
        queries: list[SearchQuery] = []

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            return _page(
                query.page, ("123", "124") if query.page == 1 else ("125",), query.page == 1
            )

    class FakeFactory:
        def create(self, **kwargs: object) -> FakeAdapter:
            assert kwargs["kind"] == SiteKind.KEEPFRDS
            assert kwargs["base_url"] == site_profile(SiteKind.KEEPFRDS).base_url
            assert kwargs["credential"] == "synthetic-cookie"
            return adapter

    async def fake_sleep(seconds: float) -> None:
        intervals.append(seconds)

    adapter = FakeAdapter()
    intervals: list[float] = []
    result = await acceptance.check_one_site(
        "keepfrds",
        config=_config(),
        factory=cast(SiteAdapterFactory, FakeFactory()),
        sleep=fake_sleep,
    )
    assert result["status"] == "PAGINATION_OK"
    assert result["page_1"] == {
        "count": 2,
        "size_known": 2,
        "date_known": 2,
        "seeders_known": 2,
        "leechers_known": 2,
    }
    assert result["overlap_count"] == 0
    assert result["requests_sent"] == 2
    assert intervals == [2.0]
    assert [query.page for query in adapter.queries] == [1, 2]
    assert all(
        query.keywords == ("2024",)
        and query.media_type is SearchMediaType.MOVIE
        and query.sort is SearchSortHint.NEWEST
        and query.page_size == 20
        for query in adapter.queries
    )
    summary = json.dumps(result)
    for private in ("synthetic-cookie", "Synthetic.Movie", "123", "124", "125"):
        assert private not in summary


@pytest.mark.asyncio
async def test_overlap_is_not_misreported_as_pagination_success() -> None:
    class FakeAdapter:
        async def search(self, query: SearchQuery) -> SearchPage:
            return _page(
                query.page, ("123",) if query.page == 1 else ("123", "456"), query.page == 1
            )

    class FakeFactory:
        def create(self, **_kwargs: object) -> FakeAdapter:
            return FakeAdapter()

    async def fake_sleep(_seconds: float) -> None:
        pass

    result = await acceptance.check_one_site(
        "keepfrds",
        config=_config(),
        factory=cast(SiteAdapterFactory, FakeFactory()),
        sleep=fake_sleep,
    )
    assert result["status"] == "PAGINATION_INCONSISTENT"
    assert result["overlap_count"] == 1
    assert "123" not in json.dumps(result)


@pytest.mark.asyncio
async def test_missing_field_in_second_page_does_not_pass_pagination() -> None:
    class FakeAdapter:
        async def search(self, query: SearchQuery) -> SearchPage:
            if query.page == 1:
                return _page(1, ("123",), True)
            return SearchPage(
                "keepfrds",
                2,
                (
                    normalize_candidate_meta(
                        site_id="keepfrds",
                        torrent_id="456",
                        display_name="Synthetic.Movie.2024.456",
                        total_size=10,
                        seeders=1,
                        leechers=1,
                    ),
                ),
                False,
            )

    class FakeFactory:
        def create(self, **_kwargs: object) -> FakeAdapter:
            return FakeAdapter()

    async def fake_sleep(_seconds: float) -> None:
        pass

    result = await acceptance.check_one_site(
        "keepfrds",
        config=_config(),
        factory=cast(SiteAdapterFactory, FakeFactory()),
        sleep=fake_sleep,
    )
    assert result["status"] == "PAGINATION_INCOMPLETE_FIELDS"
    assert result["overlap_count"] == 0
    page_2 = result["page_2"]
    assert isinstance(page_2, dict) and page_2["date_known"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cause", "expected"),
    (
        (httpx2.ConnectError("sensitive-url-and-cookie"), "NETWORK"),
        (httpx2.ReadTimeout("sensitive-url-and-cookie"), "TIMEOUT"),
        (None, "HTTP_5XX_OR_UNKNOWN"),
    ),
)
async def test_transport_failure_reports_only_stable_sanitized_category(
    cause: Exception | None,
    expected: str,
) -> None:
    class FakeAdapter:
        async def search(self, _query: SearchQuery) -> SearchPage:
            if cause is None:
                raise SiteAdapterError("SITE_UNAVAILABLE", "private-raw-response")
            raise SiteAdapterError("SITE_UNAVAILABLE", "private-raw-response") from cause

    class FakeFactory:
        def create(self, **_kwargs: object) -> FakeAdapter:
            return FakeAdapter()

    result = await acceptance.check_one_site(
        "keepfrds",
        config=_config(),
        factory=cast(SiteAdapterFactory, FakeFactory()),
    )
    assert result["status"] == "FAILED_NO_RETRY"
    assert result["failure_kind"] == expected
    assert result["stage"] == "SEARCH_PAGE_1"
    assert "sensitive" not in json.dumps(result)
    assert "private-raw-response" not in json.dumps(result)


def _synthetic_hdfans_search_html(*, candidate_id: str, next_page: bool) -> str:
    # Nine-column synthetic NexusPHP row: date/size/seeders/leechers are
    # respectively the 6th/5th/4th/3rd cells counted from the right.
    cells = [
        f'<a href="details.php?id={candidate_id}" title="Synthetic.Movie.2024">Synthetic</a>',
        "placeholder",
        "placeholder",
        "2024-01-01 12:00:00",
        "4.00 GiB",
        "8",
        "2",
        "placeholder",
        "placeholder",
    ]
    pager = '<a href="/torrents.php?search=2024&amp;page=1">next</a>' if next_page else ""
    return "<table><tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr></table>" + pager


@pytest.mark.asyncio
async def test_hdfans_real_adapter_synthetic_two_page_contract_is_isolated() -> None:
    requests: list[tuple[str, str]] = []
    cookie = "synthetic-hdfans-private-cookie"

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "hdfans.org"
        assert request.method == "GET"
        assert request.headers.get("cookie") == cookie
        assert request.url.path == "/torrents.php"
        assert request.url.params["search"] == "2024"
        assert request.url.params["sort"] == "4"
        page = request.url.params["page"]
        requests.append((request.url.path, page))
        assert page in {"0", "1"}
        return httpx2.Response(
            200,
            text=_synthetic_hdfans_search_html(
                candidate_id="123" if page == "0" else "456", next_page=page == "0"
            ),
        )

    intervals: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        intervals.append(seconds)

    result = await acceptance.check_one_site(
        "hdfans",
        config={
            "HDFans": {
                "url": site_profile(SiteKind.HDFANS).base_url,
                "auth_type": "cookie",
                "cookie": cookie,
            }
        },
        factory=SiteAdapterFactory(transport=httpx2.MockTransport(handler)),
        sleep=fake_sleep,
    )
    assert result["status"] == "PAGINATION_OK"
    first_counts, second_counts = result["page_1"], result["page_2"]
    assert isinstance(first_counts, dict) and isinstance(second_counts, dict)
    assert first_counts["count"] == second_counts["count"] == 1
    assert result["overlap_count"] == 0
    assert requests == [("/torrents.php", "0"), ("/torrents.php", "1")]
    assert intervals == [2.0]
    summary = json.dumps(result)
    for private in (cookie, "Synthetic.Movie", "123", "456"):
        assert private not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    (("http_503", "HTTP_5XX_OR_UNKNOWN"), ("connect", "NETWORK"), ("timeout", "TIMEOUT")),
)
async def test_hdfans_real_adapter_synthetic_failure_never_retries_or_leaks(
    failure: str,
    expected: str,
) -> None:
    requests: list[str] = []
    cookie = "synthetic-hdfans-private-cookie"

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        assert request.url.host == "hdfans.org"
        assert request.headers.get("cookie") == cookie
        assert request.url.path == "/torrents.php"
        if failure == "connect":
            raise httpx2.ConnectError("private-request-url")
        if failure == "timeout":
            raise httpx2.ReadTimeout("private-request-url")
        return httpx2.Response(503, text="private-response-body")

    result = await acceptance.check_one_site(
        "hdfans",
        config={
            "HDFans": {
                "url": site_profile(SiteKind.HDFANS).base_url,
                "auth_type": "cookie",
                "cookie": cookie,
            }
        },
        factory=SiteAdapterFactory(transport=httpx2.MockTransport(handler)),
    )
    assert result["status"] == "FAILED_NO_RETRY"
    assert result["stage"] == "SEARCH_PAGE_1"
    assert result["error_code"] == "SITE_UNAVAILABLE"
    assert result["failure_kind"] == expected
    assert requests == ["/torrents.php"]
    summary = json.dumps(result)
    for private in (cookie, "private-request-url", "private-response-body"):
        assert private not in summary


@pytest.mark.asyncio
async def test_absent_next_page_never_fakes_cross_page_evidence() -> None:
    class FakeAdapter:
        calls = 0

        async def search(self, _query: SearchQuery) -> SearchPage:
            self.calls += 1
            return _page(1, ("123",), False)

    class FakeFactory:
        def create(self, **_kwargs: object) -> FakeAdapter:
            return adapter

    adapter = FakeAdapter()
    result = await acceptance.check_one_site(
        "keepfrds",
        config=_config(),
        factory=cast(SiteAdapterFactory, FakeFactory()),
    )
    assert result["status"] == "PAGINATION_NOT_OBSERVED"
    assert result["requests_sent"] == adapter.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    (
        {"url": "https://outside.invalid", "auth_type": "cookie", "cookie": "synthetic-cookie"},
        {"url": "https://pt.keepfrds.com", "auth_type": "api_key", "cookie": "synthetic-cookie"},
        {"url": "https://pt.keepfrds.com", "auth_type": "cookie", "cookie": "bad\ncookie"},
    ),
)
async def test_untrusted_config_never_initializes_network_adapter(entry: dict[str, str]) -> None:
    class DeniedFactory:
        def create(self, **_kwargs: object) -> None:
            raise AssertionError("Invalid config reached adapter creation")

    result = await acceptance.check_one_site(
        "keepfrds",
        config={"KeepFrds": entry},
        factory=cast(SiteAdapterFactory, DeniedFactory()),
    )
    assert result == {"site": "keepfrds", "status": "CONFIG_BLOCKED"}


def test_live_probe_malformed_config_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_path = tmp_path / "secret"
    secret_path.write_text(json.dumps(_config("synthetic-cookie")))
    secret_path.chmod(0o600)
    monkeypatch.setattr(acceptance, "_SECRET_PATH", secret_path)

    async def fake_check(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"site": "keepfrds", "status": "PAGINATION_INCONSISTENT", "overlap_count": 1}

    monkeypatch.setattr(acceptance, "check_one_site", fake_check)
    assert acceptance.main(["--site", "keepfrds", "--live", "--acknowledge-site-search"]) == 1
    assert "synthetic-cookie" not in capsys.readouterr().out
    secret_path.chmod(0o644)
    assert acceptance.main(["--site", "keepfrds", "--live", "--acknowledge-site-search"]) == 2
