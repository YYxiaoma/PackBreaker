"""Opt-in two-page search-only evidence for pending NexusPHP site adapters.

The probe never requests torrent details or downloads, contacts a tracker or
downloader, or stores remote search results. It is deliberately excluded from
the offline default test suite and is not a production-support approval gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx2

from backend.app.domain.site_config import (
    SiteCredentialKind,
    normalize_site_credential,
    site_profile,
)
from backend.app.domain.site_search import SearchMediaType, SearchPage, SearchQuery, SearchSortHint
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from scripts.check_candidate_torrent_readonly import _SECRET_PATH, _SITE_LABELS

_NEXUS_SITES = frozenset(_SITE_LABELS) - {"rousi"}


def _page_field_counts(page: SearchPage) -> dict[str, int]:
    return {
        "count": len(page.items),
        "size_known": sum(item.total_size is not None for item in page.items),
        "date_known": sum(item.published_at is not None for item in page.items),
        "seeders_known": sum(item.seeders is not None for item in page.items),
        "leechers_known": sum(item.leechers is not None for item in page.items),
    }


def _complete_fields(counts: dict[str, int]) -> bool:
    return all(value == counts["count"] for field, value in counts.items() if field != "count")


def _safe_failure_kind(exc: Exception) -> str:
    """Classify transport failures without rendering the remote URL or exception text."""

    if getattr(exc, "code", None) != "SITE_UNAVAILABLE":
        return "OTHER"
    cause = exc.__cause__
    if isinstance(cause, httpx2.TimeoutException):
        return "TIMEOUT"
    if isinstance(cause, httpx2.NetworkError):
        return "NETWORK"
    return "HTTP_5XX_OR_UNKNOWN"


async def check_one_site(
    site: str,
    *,
    config: dict[str, Any],
    factory: SiteAdapterFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    if site not in _NEXUS_SITES:
        return {"site": site, "status": "CONFIG_BLOCKED"}
    label, kind = _SITE_LABELS[site]
    profile = site_profile(kind)
    entry = config.get(label)
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("url"), str)
        or entry["url"].rstrip("/") != profile.base_url
        or entry.get("auth_type") != "cookie"
        or not isinstance(entry.get("cookie"), str)
        or profile.credential_kind is not SiteCredentialKind.COOKIE
        or profile.support_status.value != "PENDING_ADAPTER"
    ):
        return {"site": site, "status": "CONFIG_BLOCKED"}
    try:
        cookie = normalize_site_credential(SiteCredentialKind.COOKIE, entry["cookie"])
    except ValueError:
        return {"site": site, "status": "CONFIG_BLOCKED"}

    stage = "INIT"
    try:
        adapter = (factory or SiteAdapterFactory()).create(
            kind=kind,
            base_url=profile.base_url,
            credential_kind=SiteCredentialKind.COOKIE,
            credential=cookie,
            timeout_seconds=10,
            user_agent="Mozilla/5.0",
        )
        query = SearchQuery(
            ("2024",), SearchMediaType.MOVIE, page=1, page_size=20, sort=SearchSortHint.NEWEST
        )
        stage = "SEARCH_PAGE_1"
        first = await adapter.search(query)
        first_counts = _page_field_counts(first)
        if not first.items or not first.has_more:
            return {
                "site": site,
                "status": "PAGINATION_NOT_OBSERVED",
                "page_1": first_counts,
                "page_1_has_more": first.has_more,
                "requests_sent": 1,
            }

        # No retry, no extra query variants and no torrent/details requests.
        await sleep(max(2.0, profile.search_interval_seconds))
        stage = "SEARCH_PAGE_2"
        second = await adapter.search(
            SearchQuery(
                ("2024",),
                SearchMediaType.MOVIE,
                page=2,
                page_size=20,
                sort=SearchSortHint.NEWEST,
            )
        )
        overlap = len(
            {item.identity for item in first.items} & {item.identity for item in second.items}
        )
        second_counts = _page_field_counts(second)
        if not second.items or overlap:
            status = "PAGINATION_INCONSISTENT"
        elif not _complete_fields(first_counts) or not _complete_fields(second_counts):
            status = "PAGINATION_INCOMPLETE_FIELDS"
        else:
            status = "PAGINATION_OK"
        return {
            "site": site,
            "status": status,
            "page_1": first_counts,
            "page_1_has_more": first.has_more,
            "page_2": second_counts,
            "page_2_has_more": second.has_more,
            "overlap_count": overlap,
            "requested_page_size": 20,
            "requests_sent": 2,
        }
    except Exception as exc:
        # Never include a third-party response, signed URL, torrent ID or Cookie.
        code = getattr(exc, "code", None)
        return {
            "site": site,
            "status": "FAILED_NO_RETRY",
            "stage": stage,
            "error_code": code if isinstance(code, str) and code.isidentifier() else "UNCLASSIFIED",
            "error_class": type(exc).__name__,
            "failure_kind": _safe_failure_kind(exc),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候选 NexusPHP 站点两页只读搜索验收")
    parser.add_argument("--site", choices=sorted(_NEXUS_SITES), required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-site-search", action="store_true")
    args = parser.parse_args(argv)
    if not (args.live and args.acknowledge_site_search):
        print("未发起站点请求：须同时指定 --live 与 --acknowledge-site-search")
        return 2
    try:
        path: Path = _SECRET_PATH
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            print("配置文件不存在或权限不为 0600；未发起站点请求")
            return 2
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            print("配置文件格式不正确；未发起站点请求")
            return 2
    except (OSError, ValueError, UnicodeError):
        print("配置文件无法安全读取；未发起站点请求")
        return 2
    result = asyncio.run(check_one_site(args.site, config=config))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "PAGINATION_OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
