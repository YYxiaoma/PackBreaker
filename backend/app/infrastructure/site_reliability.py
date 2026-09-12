from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import TypeVar, cast
from weakref import WeakValueDictionary

from backend.app.domain.site_adapter import (
    SiteAdapter,
    SiteConnectionResult,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_search import SearchPage, SearchQuery, SiteSearchCapabilities
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError

_T = TypeVar("_T")
_CACHE_MISS = object()


@dataclass(frozen=True, slots=True)
class SiteReliabilityPolicy:
    """站点逻辑调用的进程内可靠性策略；不包含任何凭证或站点私有响应。"""

    max_concurrency: int = 1
    max_attempts: int = 3
    retry_deadline_seconds: float = 300.0
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 8.0
    retry_jitter_ratio: float = 0.2
    circuit_failure_threshold: int = 3
    circuit_open_seconds: float = 30.0
    search_cache_ttl_seconds: float = 60.0
    details_cache_ttl_seconds: float = 300.0
    cache_max_entries: int = 256

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("站点并发上限必须大于 0")
        if self.max_attempts < 1:
            raise ValueError("站点最大尝试次数必须大于 0")
        if self.retry_deadline_seconds <= 0:
            raise ValueError("站点重试 deadline 必须大于 0")
        if self.retry_base_seconds < 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("站点退避时间范围无效")
        if not 0 <= self.retry_jitter_ratio <= 1:
            raise ValueError("站点退避 jitter ratio 必须位于 0..1")
        if self.circuit_failure_threshold < 1 or self.circuit_open_seconds <= 0:
            raise ValueError("站点熔断策略无效")
        if self.search_cache_ttl_seconds < 0 or self.details_cache_ttl_seconds < 0:
            raise ValueError("站点缓存 TTL 不能为负数")
        if self.cache_max_entries < 1:
            raise ValueError("站点缓存条目上限必须大于 0")


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    value: object
    expires_at: float


class _SiteReliabilityState:
    def __init__(self, policy: SiteReliabilityPolicy) -> None:
        self.semaphore = asyncio.Semaphore(policy.max_concurrency)
        self.rate_lock = asyncio.Lock()
        self.breaker_lock = asyncio.Lock()
        self.cache_lock = asyncio.Lock()
        self.capabilities_lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.failure_count = 0
        self.open_until: float | None = None
        self.half_open_in_flight = False
        self.capabilities: SiteSearchCapabilities | None = None
        self.cache: OrderedDict[Hashable, _CacheEntry] = OrderedDict()
        self.cache_key_locks: WeakValueDictionary[Hashable, asyncio.Lock] = WeakValueDictionary()

    def cache_key_lock(self, key: Hashable) -> asyncio.Lock:
        existing = self.cache_key_locks.get(key)
        if existing is not None:
            return existing
        created = asyncio.Lock()
        self.cache_key_locks[key] = created
        return created


class SiteReliabilityRegistry:
    """按站点配置 ID + version 共享限流、缓存与熔断状态。"""

    def __init__(
        self,
        policy: SiteReliabilityPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self._policy = policy or SiteReliabilityPolicy()
        self._clock = clock
        self._sleep = sleep
        self._random = random_fn
        self._states: dict[tuple[str, int], _SiteReliabilityState] = {}

    def wrap(
        self,
        *,
        config_id: str,
        config_version: int,
        adapter: SiteAdapter,
    ) -> SiteAdapter:
        if not config_id.strip() or config_version < 1:
            raise ValueError("站点可靠性绑定必须包含有效配置 ID 与 version")
        key = (config_id, config_version)
        for stale_key in tuple(self._states):
            if stale_key[0] == config_id and stale_key != key:
                del self._states[stale_key]
        state = self._states.setdefault(key, _SiteReliabilityState(self._policy))
        return ReliableSiteAdapter(
            adapter,
            state,
            self._policy,
            clock=self._clock,
            sleep=self._sleep,
            random_fn=self._random,
        )


class ReliableSiteAdapter:
    """统一实现站点调用限流、有限重试、缓存与熔断；保持 SiteAdapter 契约。"""

    def __init__(
        self,
        delegate: SiteAdapter,
        state: _SiteReliabilityState,
        policy: SiteReliabilityPolicy,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        random_fn: Callable[[], float],
    ) -> None:
        self._delegate = delegate
        self._state = state
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._random = random_fn

    async def capabilities(self) -> SiteSearchCapabilities:
        if self._state.capabilities is not None:
            return self._state.capabilities
        async with self._state.capabilities_lock:
            if self._state.capabilities is None:
                self._state.capabilities = await self._delegate.capabilities()
            return self._state.capabilities

    async def test_connection(self) -> SiteConnectionResult:
        return await self._invoke(self._delegate.test_connection)

    async def search(self, query: SearchQuery) -> SearchPage:
        key: Hashable = ("search", query)
        return await self._cached(
            key,
            self._policy.search_cache_ttl_seconds,
            lambda: self._delegate.search(query),
            SearchPage,
        )

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        normalized_id = torrent_id.strip()
        key: Hashable = ("details", normalized_id)
        return await self._cached(
            key,
            self._policy.details_cache_ttl_seconds,
            lambda: self._delegate.fetch_details(torrent_id),
            TorrentDetails,
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        # torrent bytes 是执行安全证据。这里故意不缓存，确保 reverify / execution plan / ADDING
        # 每次都重新读取远端 payload，并由上层继续核对 metainfo digest。
        return await self._invoke(
            lambda: self._delegate.fetch_torrent(torrent_id),
            retry_allowed=False,
        )

    async def _cached(
        self,
        key: Hashable,
        ttl_seconds: float,
        operation: Callable[[], Awaitable[_T]],
        expected_type: type[_T],
    ) -> _T:
        cached = await self._cache_get(key)
        if cached is not _CACHE_MISS:
            return cast(_T, cached)
        if ttl_seconds <= 0:
            return await self._invoke(operation)

        async with self._state.cache_key_lock(key):
            cached = await self._cache_get(key)
            if cached is not _CACHE_MISS:
                return cast(_T, cached)
            result = await self._invoke(operation)
            if not isinstance(result, expected_type):
                raise TypeError("站点适配器返回类型不符合 SiteAdapter 契约")
            await self._cache_put(key, result, ttl_seconds)
            return result

    async def _invoke(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        retry_allowed: bool = True,
    ) -> _T:
        capabilities = await self.capabilities()
        last_error: SiteAdapterError | None = None
        deadline = self._clock() + self._policy.retry_deadline_seconds
        attempt_limit = self._policy.max_attempts if retry_allowed else 1
        for attempt in range(1, attempt_limit + 1):
            await self._reject_open_circuit()
            async with self._state.semaphore:
                rate_deadline = deadline if attempt > 1 else None
                if not await self._wait_for_rate_slot(
                    capabilities.min_request_interval_seconds,
                    deadline=rate_deadline,
                ):
                    break
                half_open_probe = await self._begin_attempt()
                try:
                    result = await operation()
                except asyncio.CancelledError:
                    await self._abort_attempt(half_open_probe)
                    raise
                except SiteAdapterError as exc:
                    if not exc.retryable:
                        await self._record_nonretryable_failure(exc, half_open_probe)
                        raise
                    last_error = exc
                    await self._record_breaker_failure(half_open_probe)
                else:
                    await self._record_success()
                    return result

            if attempt >= attempt_limit:
                break
            if last_error is None:
                raise RuntimeError("站点重试状态缺少失败原因")
            delay = self._retry_delay(attempt, last_error.retry_after_seconds)
            if self._clock() + delay > deadline:
                break
            await self._sleep(delay)

        if last_error is None:
            raise RuntimeError("站点调用未返回结果且没有失败原因")
        raise last_error

    async def _reject_open_circuit(self) -> None:
        async with self._state.breaker_lock:
            open_until = self._state.open_until
            if open_until is None:
                return
            now = self._clock()
            if now < open_until:
                raise _circuit_open(open_until - now)
            if self._state.half_open_in_flight:
                raise _circuit_open(1.0)

    async def _begin_attempt(self) -> bool:
        async with self._state.breaker_lock:
            open_until = self._state.open_until
            if open_until is None:
                return False
            now = self._clock()
            if now < open_until:
                raise _circuit_open(open_until - now)
            if self._state.half_open_in_flight:
                raise _circuit_open(1.0)
            self._state.half_open_in_flight = True
            return True

    async def _abort_attempt(self, half_open_probe: bool) -> None:
        if not half_open_probe:
            return
        async with self._state.breaker_lock:
            self._state.half_open_in_flight = False

    async def _record_success(self) -> None:
        async with self._state.breaker_lock:
            self._state.failure_count = 0
            self._state.open_until = None
            self._state.half_open_in_flight = False

    async def _record_nonretryable_failure(
        self,
        error: SiteAdapterError,
        half_open_probe: bool,
    ) -> None:
        if error.code == "SITE_AUTH_FAILED":
            await self._open_circuit_immediately()
            return
        if error.code == "SITE_INVALID_RESPONSE":
            await self._record_breaker_failure(half_open_probe)
            return
        await self._record_success()

    async def _open_circuit_immediately(self) -> None:
        async with self._state.breaker_lock:
            self._state.failure_count = self._policy.circuit_failure_threshold
            self._state.open_until = self._clock() + self._policy.circuit_open_seconds
            self._state.half_open_in_flight = False

    async def _record_breaker_failure(self, half_open_probe: bool) -> None:
        async with self._state.breaker_lock:
            if half_open_probe:
                self._state.failure_count = self._policy.circuit_failure_threshold
                self._state.open_until = self._clock() + self._policy.circuit_open_seconds
                self._state.half_open_in_flight = False
                return
            self._state.failure_count += 1
            if self._state.failure_count >= self._policy.circuit_failure_threshold:
                self._state.open_until = self._clock() + self._policy.circuit_open_seconds
                self._state.half_open_in_flight = False

    async def _wait_for_rate_slot(
        self,
        interval_seconds: float,
        *,
        deadline: float | None,
    ) -> bool:
        if interval_seconds <= 0:
            return True
        async with self._state.rate_lock:
            now = self._clock()
            scheduled = max(now, self._state.next_request_at)
            delay = max(0.0, scheduled - now)
            if deadline is not None and now + delay > deadline:
                return False
            self._state.next_request_at = scheduled + interval_seconds
        if delay > 0:
            await self._sleep(delay)
        return True

    def _retry_delay(self, attempt: int, retry_after_seconds: float | None) -> float:
        base = float(
            min(
                self._policy.retry_max_seconds,
                self._policy.retry_base_seconds * (2 ** max(0, attempt - 1)),
            )
        )
        random_value = min(1.0, max(0.0, self._random()))
        jitter_multiplier = 1 + self._policy.retry_jitter_ratio * (2 * random_value - 1)
        backoff = max(0.0, base * jitter_multiplier)
        if retry_after_seconds is not None:
            backoff = max(backoff, retry_after_seconds)
        return backoff

    async def _cache_get(self, key: Hashable) -> object:
        async with self._state.cache_lock:
            entry = self._state.cache.get(key)
            if entry is None:
                return _CACHE_MISS
            if entry.expires_at <= self._clock():
                del self._state.cache[key]
                return _CACHE_MISS
            self._state.cache.move_to_end(key)
            return entry.value

    async def _cache_put(self, key: Hashable, value: object, ttl_seconds: float) -> None:
        async with self._state.cache_lock:
            self._state.cache[key] = _CacheEntry(value, self._clock() + ttl_seconds)
            self._state.cache.move_to_end(key)
            while len(self._state.cache) > self._policy.cache_max_entries:
                self._state.cache.popitem(last=False)


def _circuit_open(retry_after_seconds: float) -> SiteAdapterError:
    return SiteAdapterError(
        "SITE_CIRCUIT_OPEN",
        "站点暂时处于熔断保护状态",
        retryable=True,
        retry_after_seconds=max(0.0, retry_after_seconds),
    )
