"""Explicitly gated one-shot, in-memory real torrent acceptance for pending sites.

The read may be counted by a private tracker as a torrent download, even
though this script never starts a client or contacts a tracker. It is NOT part
of the default test suite and cannot run without two affirmative CLI flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from backend.app.domain.site_config import (
    SiteCredentialKind,
    SiteKind,
    normalize_site_credential,
    site_profile,
)
from backend.app.domain.site_search import SearchMediaType, SearchQuery
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.torrent_parser import parse_torrent

_SECRET_PATH = Path(__file__).resolve().parents[1] / "runtime/site-acceptance.secret"
_SITE_LABELS: dict[str, tuple[str, SiteKind]] = {
    "hdhome": ("HDHome", SiteKind.HDHOME),
    "keepfrds": ("KeepFrds", SiteKind.KEEPFRDS),
    "ubits": ("UBits", SiteKind.UBITS),
    "hdfans": ("HDFans", SiteKind.HDFANS),
    "btschool": ("BTSchool", SiteKind.BTSCHOOL),
    "pttime": ("PTTime", SiteKind.PTTIME),
    "lingyin": ("聆音Club", SiteKind.LINGYIN_CLUB),
    "rousi": ("Rousi Pro", SiteKind.ROUSI_PRO),
}


async def check_one_site(
    site: str,
    *,
    config: dict[str, Any],
    factory: SiteAdapterFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    label, kind = _SITE_LABELS[site]
    profile = site_profile(kind)
    entry = config.get(label)
    dual_credential = kind is SiteKind.ROUSI_PRO
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("url"), str)
        or entry["url"].rstrip("/") != profile.base_url
        or entry.get("auth_type") not in ({"cookie", "api_key"} if dual_credential else {"cookie"})
        or not isinstance(entry.get("cookie"), str)
        or not entry["cookie"].strip()
        or (
            dual_credential
            and (not isinstance(entry.get("api_key"), str) or not entry["api_key"].strip())
        )
        or profile.support_status.value not in {"PENDING_ADAPTER", "PENDING_REAL_VALIDATION"}
    ):
        return {"site": site, "status": "CONFIG_BLOCKED"}
    if dual_credential:
        try:
            # The main Rousi credential is the search API key, never the
            # download Cookie. Reject malformed values before any network I/O.
            search_credential = normalize_site_credential(
                SiteCredentialKind.API_KEY, entry["api_key"]
            )
            download_cookie = normalize_site_credential(SiteCredentialKind.COOKIE, entry["cookie"])
        except ValueError:
            return {"site": site, "status": "CONFIG_BLOCKED"}
    else:
        search_credential = entry["cookie"]
        download_cookie = None
    stage = "INIT"
    try:
        adapter = (factory or SiteAdapterFactory()).create(
            kind=kind,
            base_url=profile.base_url,
            credential_kind=profile.credential_kind,
            credential=search_credential,
            download_cookie=download_cookie,
            timeout_seconds=10,
            user_agent="Mozilla/5.0",
        )
        stage = "SEARCH"
        page = await adapter.search(
            SearchQuery(("2024",), SearchMediaType.MOVIE, page=1, page_size=20)
        )
        candidate = next(
            (
                item
                for item in page.items
                if item.total_size is not None and (item.seeders or 0) > 0
            ),
            None,
        )
        if candidate is None:
            return {"site": site, "status": "NO_ELIGIBLE_CANDIDATE"}
        # Keep the explicit profile interval between search and details. No
        # retry or extra candidate on any failed torrent fetch.
        await sleep(max(2.0, profile.search_interval_seconds))
        stage = "FETCH_TORRENT"
        result = await adapter.fetch_torrent(candidate.torrent_id)
        stage = "PARSE_METAINFO"
        parsed = parse_torrent(result.content)
        return {
            "site": site,
            "status": "VALID_METAINFO",
            "torrent_kind": parsed.torrent_kind.value,
            "file_count": len(parsed.files),
            "bytes_band": (
                "le_128k"
                if len(result.content) <= 131072
                else "le_1m"
                if len(result.content) <= 1048576
                else "over_1m"
            ),
            "client_or_tracker_contacted": False,
            "torrent_saved": False,
        }
    except Exception as exc:
        # Raw exception text can contain signed URLs. Only stable machine
        # codes and exception class names may leave this isolated process.
        code = getattr(exc, "code", None)
        return {
            "site": site,
            "status": "FAILED_NO_RETRY",
            "stage": stage,
            "error_code": code if isinstance(code, str) and code.isidentifier() else "UNCLASSIFIED",
            "error_class": type(exc).__name__,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="单站只读 Torrent 元信息验收（可能计入站点下载记录）"
    )
    parser.add_argument("--site", choices=tuple(_SITE_LABELS), required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-download-record", action="store_true")
    args = parser.parse_args(argv)
    if not (args.live and args.acknowledge_download_record):
        print("未发起站点请求：须同时指定 --live 与 --acknowledge-download-record")
        return 2
    try:
        if not _SECRET_PATH.is_file() or stat.S_IMODE(_SECRET_PATH.stat().st_mode) != 0o600:
            print("配置文件不存在或权限不为 0600；未发起站点请求")
            return 2
        config = json.loads(_SECRET_PATH.read_text())
        if not isinstance(config, dict):
            print("配置文件格式不正确；未发起站点请求")
            return 2
    except (OSError, ValueError, UnicodeError):
        print("配置文件无法安全读取；未发起站点请求")
        return 2
    result = asyncio.run(check_one_site(args.site, config=config))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "VALID_METAINFO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
