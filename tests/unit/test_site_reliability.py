from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx2
import pytest

from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.sites import MTeamAdapter
from backend.app.infrastructure.site_reliability import (
    SiteReliabilityPolicy,
    SiteReliabilityRegistry,
)


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSiteAdapter:
    def __init__(self, *, interval: float = 0.0) -> None:
        self.interval = interval
        self.search_calls = 0
        self.details_calls = 0
        self.torrent_calls = 0
        self.search_times: list[float] = []
        self.clock: _FakeTime | None = None
        self.search_failures: list[SiteAdapterError] = []
        self.torrent_failures: list[SiteAdapterError] = []
        self.cancel_next_search = False
        self.search_started: asyncio.Event | None = None
        self.release_search: asyncio.Event | None = None

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(min_request_interval_seconds=self.interval)

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

    async def search(self, query: SearchQuery) -> SearchPage:
        self.search_calls += 1
        if self.clock is not None:
            self.search_times.append(self.clock.monotonic())
        if self.cancel_next_search:
            self.cancel_next_search = False
            raise asyncio.CancelledError
        if self.search_failures:
            raise self.search_failures.pop(0)
        if self.search_started is not None:
            self.search_started.set()
            if self.release_search is None:
                raise RuntimeError("blocking search 缺少 release event")
            await self.release_search.wait()
        await asyncio.sleep(0)
        candidate = normalize_candidate_meta(
            site_id="fake",
            torrent_id=str(self.search_calls),
            display_name=query.query_text or "synthetic",
        )
        return SearchPage("fake", query.page, (candidate,), False, 1)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        self.details_calls += 1
        return TorrentDetails(
            normalize_candidate_meta(
                site_id="fake",
                torrent_id=torrent_id,
                display_name=f"Synthetic.{torrent_id}",
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        self.torrent_calls += 1
        if self.torrent_failures:
            raise self.torrent_failures.pop(0)
        return TorrentPayload(
            "fake",
            torrent_id,
            f"torrent-{torrent_id}-{self.torrent_calls}".encode(),
            datetime.now(UTC),
        )


def _registry(
    fake_time: _FakeTime,
    *,
    policy: SiteReliabilityPolicy | None = None,
) -> SiteReliabilityRegistry:
    return SiteReliabilityRegistry(
        policy,
        clock=fake_time.monotonic,
        sleep=fake_time.sleep,
        random_fn=lambda: 0.5,
    )


@pytest.mark.asyncio
async def test_site_reliability_cache_singleflight_rate_limit_and_uncached_torrent() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter(interval=2.0)
    raw.clock = fake_time
    adapter = _registry(fake_time).wrap(config_id="site-1", config_version=1, adapter=raw)
    query = SearchQuery(("synthetic",), SearchMediaType.MOVIE)

    first, second = await asyncio.gather(*(adapter.search(query) for _ in range(2)))
    assert first == second
    assert raw.search_calls == 1
    assert fake_time.sleeps == []

    other = SearchQuery(("other",), SearchMediaType.MOVIE)
    await adapter.search(other)
    assert raw.search_calls == 2
    assert raw.search_times == [0.0, 2.0]
    assert fake_time.sleeps == [2.0]

    details_first = await adapter.fetch_details("42")
    details_second = await adapter.fetch_details("42")
    assert details_first == details_second
    assert raw.details_calls == 1

    torrent_first = await adapter.fetch_torrent("42")
    torrent_second = await adapter.fetch_torrent("42")
    assert torrent_first.content != torrent_second.content
    assert raw.torrent_calls == 2
    assert fake_time.sleeps == [2.0, 2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_site_reliability_retries_retryable_failure_with_jitter_and_retry_after() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.search_failures.append(
        SiteAdapterError(
            "SITE_RATE_LIMITED",
            "synthetic limited",
            retryable=True,
            retry_after_seconds=4.0,
        )
    )
    policy = SiteReliabilityPolicy(
        max_attempts=3,
        retry_base_seconds=0.5,
        retry_max_seconds=8.0,
        retry_jitter_ratio=0.2,
        circuit_failure_threshold=5,
    )
    adapter = _registry(fake_time, policy=policy).wrap(
        config_id="site-retry", config_version=1, adapter=raw
    )

    result = await adapter.search(SearchQuery(("retry",), SearchMediaType.MOVIE))

    assert result.items
    assert raw.search_calls == 2
    assert fake_time.sleeps == [4.0]


@pytest.mark.asyncio
async def test_site_reliability_circuit_opens_then_half_open_success_recovers() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.search_failures.extend(
        [
            SiteAdapterError("SITE_UNAVAILABLE", "first", retryable=True),
            SiteAdapterError("SITE_UNAVAILABLE", "second", retryable=True),
        ]
    )
    policy = SiteReliabilityPolicy(
        max_attempts=1,
        circuit_failure_threshold=2,
        circuit_open_seconds=10.0,
        search_cache_ttl_seconds=0.0,
    )
    registry = _registry(fake_time, policy=policy)
    adapter = registry.wrap(config_id="site-breaker", config_version=1, adapter=raw)
    query = SearchQuery(("breaker",), SearchMediaType.MOVIE)

    with pytest.raises(SiteAdapterError) as first:
        await adapter.search(query)
    with pytest.raises(SiteAdapterError) as second:
        await adapter.search(query)
    with pytest.raises(SiteAdapterError) as opened:
        await adapter.search(query)

    assert first.value.code == second.value.code == "SITE_UNAVAILABLE"
    assert opened.value.code == "SITE_CIRCUIT_OPEN"
    assert opened.value.retry_after_seconds == pytest.approx(10.0)
    assert raw.search_calls == 2

    fake_time.advance(10.0)
    recovered = await adapter.search(query)
    assert recovered.items
    assert raw.search_calls == 3

    await adapter.search(query)
    assert raw.search_calls == 4


@pytest.mark.asyncio
async def test_site_reliability_half_open_allows_only_one_probe() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.search_failures.append(SiteAdapterError("SITE_UNAVAILABLE", "down", retryable=True))
    policy = SiteReliabilityPolicy(
        max_concurrency=2,
        max_attempts=1,
        circuit_failure_threshold=1,
        circuit_open_seconds=5.0,
        search_cache_ttl_seconds=0.0,
    )
    adapter = _registry(fake_time, policy=policy).wrap(
        config_id="site-half-open", config_version=1, adapter=raw
    )
    query = SearchQuery(("half-open",), SearchMediaType.MOVIE)

    with pytest.raises(SiteAdapterError):
        await adapter.search(query)
    fake_time.advance(5.0)
    raw.search_started = asyncio.Event()
    raw.release_search = asyncio.Event()
    probe = asyncio.create_task(adapter.search(query))
    await asyncio.wait_for(raw.search_started.wait(), timeout=1)

    with pytest.raises(SiteAdapterError) as concurrent:
        await adapter.search(SearchQuery(("concurrent",), SearchMediaType.MOVIE))
    assert concurrent.value.code == "SITE_CIRCUIT_OPEN"
    assert raw.search_calls == 2

    raw.release_search.set()
    assert (await probe).items
    assert raw.search_calls == 2


@pytest.mark.asyncio
async def test_site_reliability_invalid_response_counts_toward_circuit_without_retry() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.search_failures.extend(
        [
            SiteAdapterError("SITE_INVALID_RESPONSE", "bad payload one"),
            SiteAdapterError("SITE_INVALID_RESPONSE", "bad payload two"),
        ]
    )
    policy = SiteReliabilityPolicy(
        circuit_failure_threshold=2,
        circuit_open_seconds=7.0,
        search_cache_ttl_seconds=0.0,
    )
    adapter = _registry(fake_time, policy=policy).wrap(
        config_id="site-invalid", config_version=1, adapter=raw
    )
    query = SearchQuery(("invalid",), SearchMediaType.MOVIE)

    for _ in range(2):
        with pytest.raises(SiteAdapterError) as failure:
            await adapter.search(query)
        assert failure.value.code == "SITE_INVALID_RESPONSE"

    with pytest.raises(SiteAdapterError) as opened:
        await adapter.search(query)
    assert opened.value.code == "SITE_CIRCUIT_OPEN"
    assert raw.search_calls == 2
    assert fake_time.sleeps == []


@pytest.mark.asyncio
async def test_site_reliability_config_version_resets_old_breaker_and_cache_state() -> None:
    fake_time = _FakeTime()
    policy = SiteReliabilityPolicy(
        max_attempts=1,
        circuit_failure_threshold=1,
        circuit_open_seconds=60.0,
        search_cache_ttl_seconds=60.0,
    )
    registry = _registry(fake_time, policy=policy)
    failing = _FakeSiteAdapter()
    failing.search_failures.append(SiteAdapterError("SITE_UNAVAILABLE", "down", retryable=True))
    version_one = registry.wrap(config_id="same-site", config_version=1, adapter=failing)
    query = SearchQuery(("versioned",), SearchMediaType.MOVIE)

    with pytest.raises(SiteAdapterError):
        await version_one.search(query)

    healthy = _FakeSiteAdapter()
    version_two = registry.wrap(config_id="same-site", config_version=2, adapter=healthy)
    assert (await version_two.search(query)).items
    assert healthy.search_calls == 1


@pytest.mark.asyncio
async def test_site_reliability_does_not_retry_nonretryable_error() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.search_failures.append(SiteAdapterError("SITE_AUTH_FAILED", "bad credential"))
    adapter = _registry(fake_time).wrap(config_id="site-auth", config_version=1, adapter=raw)

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.search(SearchQuery(("auth",), SearchMediaType.MOVIE))

    assert failure.value.code == "SITE_AUTH_FAILED"
    assert raw.search_calls == 1
    assert fake_time.sleeps == []

    with pytest.raises(SiteAdapterError) as opened:
        await adapter.search(SearchQuery(("auth-other",), SearchMediaType.MOVIE))
    assert opened.value.code == "SITE_CIRCUIT_OPEN"
    assert raw.search_calls == 1


@pytest.mark.asyncio
async def test_site_reliability_does_not_retry_torrent_token_flow() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.torrent_failures.append(
        SiteAdapterError("SITE_UNAVAILABLE", "token flow failed", retryable=True)
    )
    adapter = _registry(fake_time).wrap(config_id="site-torrent", config_version=1, adapter=raw)

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.fetch_torrent("42")

    assert failure.value.code == "SITE_UNAVAILABLE"
    assert raw.torrent_calls == 1
    assert fake_time.sleeps == []


@pytest.mark.asyncio
async def test_site_reliability_retry_schedule_respects_total_deadline() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter(interval=10.0)
    raw.search_failures.extend(
        [
            SiteAdapterError("SITE_UNAVAILABLE", "first", retryable=True),
            SiteAdapterError("SITE_UNAVAILABLE", "second", retryable=True),
        ]
    )
    policy = SiteReliabilityPolicy(
        max_attempts=3,
        retry_deadline_seconds=5.0,
        retry_base_seconds=0.5,
        retry_max_seconds=1.0,
        circuit_failure_threshold=5,
        search_cache_ttl_seconds=0.0,
    )
    adapter = _registry(fake_time, policy=policy).wrap(
        config_id="site-deadline", config_version=1, adapter=raw
    )

    with pytest.raises(SiteAdapterError) as failure:
        await adapter.search(SearchQuery(("deadline",), SearchMediaType.MOVIE))

    assert failure.value.code == "SITE_UNAVAILABLE"
    assert raw.search_calls == 1
    assert fake_time.sleeps == [0.5]


@pytest.mark.asyncio
async def test_site_reliability_cancelled_half_open_probe_does_not_stick_breaker() -> None:
    fake_time = _FakeTime()
    raw = _FakeSiteAdapter()
    raw.search_failures.append(SiteAdapterError("SITE_UNAVAILABLE", "down", retryable=True))
    policy = SiteReliabilityPolicy(
        max_attempts=1,
        circuit_failure_threshold=1,
        circuit_open_seconds=5.0,
        search_cache_ttl_seconds=0.0,
    )
    adapter = _registry(fake_time, policy=policy).wrap(
        config_id="site-cancel", config_version=1, adapter=raw
    )
    query = SearchQuery(("cancel",), SearchMediaType.MOVIE)

    with pytest.raises(SiteAdapterError):
        await adapter.search(query)
    fake_time.advance(5.0)
    raw.cancel_next_search = True
    with pytest.raises(asyncio.CancelledError):
        await adapter.search(query)

    recovered = await adapter.search(query)
    assert recovered.items
    assert raw.search_calls == 3


@pytest.mark.asyncio
async def test_mteam_mocktransport_uses_shared_retry_and_rate_limit_without_secret_leak() -> None:
    fake_time = _FakeTime()
    api_key = "PACKBREAKER-RELIABILITY-CANARY-91d4"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(503)
        return httpx2.Response(200, json={"code": "0", "data": {"data": [], "total": "0"}})

    raw = MTeamAdapter(api_key, transport=httpx2.MockTransport(handler))
    policy = SiteReliabilityPolicy(
        max_attempts=2,
        retry_base_seconds=0.5,
        retry_max_seconds=0.5,
        retry_jitter_ratio=0.0,
        circuit_failure_threshold=3,
        search_cache_ttl_seconds=0.0,
    )
    adapter = _registry(fake_time, policy=policy).wrap(
        config_id="mteam-config", config_version=1, adapter=raw
    )

    page = await adapter.search(SearchQuery(("synthetic",), SearchMediaType.MOVIE))

    assert page.site_id == "mteam"
    assert len(requests) == 2
    assert fake_time.sleeps == [0.5, 89.5]
    assert all(request.headers.get("x-api-key") == api_key for request in requests)
    assert api_key not in repr(adapter)
