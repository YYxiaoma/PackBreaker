"""Strict real-movie cross-seed evidence, without materialization or downloader writes.

Only execute against explicitly authorized sites. Searching is read-only at the
site level; fetching a torrent may register a download in the private tracker.
Never prints release names, torrent IDs, filesystem paths, URLs or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

from backend.app.application.unpack_matching import _task_unit_from_item
from backend.app.domain.candidate_scoring import candidate_search_relevant
from backend.app.domain.media_matching import tokenize_title
from backend.app.domain.site_config import SiteCredentialKind, SiteKind, site_profile
from backend.app.domain.site_search import SearchMediaType, SearchQuery
from backend.app.domain.torrent import TorrentKind
from backend.app.domain.unpack_matching import assess_unpack_candidate
from backend.app.domain.verification import (
    FileMappingState,
    HybridVerificationResult,
    PieceStatus,
    TorrentVerificationResult,
    V1VerificationResult,
    V2VerificationResult,
    VerificationLevel,
)
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.persistence.models import UnpackExecutionItem
from backend.app.infrastructure.piece_verifier import (
    V1FileMapping,
    verify_hybrid,
    verify_v1_pieces,
    verify_v2_files,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot
from backend.app.infrastructure.torrent_parser import parse_torrent

_DEFAULT_SECRET_FILE = Path(__file__).resolve().parents[1] / "runtime/site-acceptance.secret"
_DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "runtime/data"
_COOKIE_SITES: dict[str, tuple[str, SiteKind]] = {
    "hdhome": ("HDHome", SiteKind.HDHOME),
    "keepfrds": ("KeepFrds", SiteKind.KEEPFRDS),
    "ubits": ("UBits", SiteKind.UBITS),
    "hdfans": ("HDFans", SiteKind.HDFANS),
    "btschool": ("BTSchool", SiteKind.BTSCHOOL),
    "pttime": ("PTTime", SiteKind.PTTIME),
    "lingyin": ("聆音Club", SiteKind.LINGYIN_CLUB),
    "pterclub": ("PTerClub", SiteKind.PTERCLUB),
    "audiences": ("Audiences", SiteKind.AUDIENCES),
    "springsunday": ("SpringSunday", SiteKind.SPRING_SUNDAY),
    "hddolby": ("HDDolby", SiteKind.HDDOLBY),
    "u2": ("U2", SiteKind.U2),
    "tangpt": ("不可躺", SiteKind.TANGPT),
    "carpt": ("CarPT", SiteKind.CARPT),
}
_VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".ts"})
_MAX_RELATIVE_SIZE_DIFF = 0.01


def _safe_file(path: Path, root: Path) -> bool:
    """Reject symlinks, paths outside the opted-in root, and non-regular files."""
    if path.is_symlink() or root.is_symlink():
        return False
    try:
        relative = path.absolute().relative_to(root.absolute())
        if not relative.parts or ".." in relative.parts:
            return False
        chain = root
        for segment in relative.parts:
            chain = chain / segment
            if chain.is_symlink():
                return False
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except (OSError, ValueError):
        return False


def _map_existing_files(
    files: tuple[Any, ...], source: Path, data_root: Path
) -> tuple[V1FileMapping, ...]:
    mappings: list[V1FileMapping] = []
    for item in files:
        if item.padding:
            mappings.append(V1FileMapping(item.path, FileMappingState.PADDING))
            continue
        if item.zero_length:
            mappings.append(V1FileMapping(item.path, FileMappingState.ZERO_LENGTH))
            continue
        basename = PurePosixPath(item.path).name
        valid: dict[str, Path] = {}
        for parent in (source.parent, source.parent.parent):
            candidate = parent / basename
            if _safe_file(candidate, data_root) and candidate.stat().st_size == item.length:
                valid[str(candidate.resolve(strict=True))] = candidate
        if len(valid) == 1:
            mappings.append(
                V1FileMapping(item.path, FileMappingState.MAPPED, next(iter(valid.values())))
            )
        else:
            state = FileMappingState.AMBIGUOUS if valid else FileMappingState.MISSING
            mappings.append(V1FileMapping(item.path, state))
    return tuple(mappings)


def _verified_piece_counts(
    result: V1VerificationResult | V2VerificationResult | HybridVerificationResult,
) -> tuple[int, int]:
    if isinstance(result, HybridVerificationResult):
        v1, _ = _verified_piece_counts(result.v1)
        return v1, len(result.v1.pieces)
    if isinstance(result, V1VerificationResult):
        return sum(piece.status is PieceStatus.VERIFIED for piece in result.pieces), len(
            result.pieces
        )
    pieces = tuple(piece for file in result.files for piece in file.pieces)
    return sum(piece.status is PieceStatus.VERIFIED for piece in pieces), len(pieces)


async def check_movie_once(
    site: str,
    *,
    config: dict[str, Any],
    source_path: Path,
    data_root: Path,
    query_text: str,
    search_only: bool,
    factory: SiteAdapterFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    label, kind = _COOKIE_SITES[site]
    profile = site_profile(kind)
    entry = config.get(label)
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("url"), str)
        or entry["url"].rstrip("/") != profile.base_url
        or entry.get("auth_type") != "cookie"
        or not isinstance(entry.get("cookie"), str)
        or not entry["cookie"].strip()
        or profile.credential_kind is not SiteCredentialKind.COOKIE
    ):
        return {"site": site, "status": "CONFIG_BLOCKED"}
    if (
        not data_root.is_dir()
        or not _safe_file(source_path, data_root)
        or source_path.suffix.casefold() not in _VIDEO_EXTENSIONS
    ):
        return {"site": site, "status": "SOURCE_BLOCKED"}
    keywords = tokenize_title(query_text)
    if not keywords or len(query_text) > 120:
        return {"site": site, "status": "QUERY_BLOCKED"}

    stage = "SEARCH"
    try:
        snapshot = current_file_snapshot(source_path)
        unit = _task_unit_from_item(
            cast(
                UnpackExecutionItem,
                SimpleNamespace(
                    source_object_key="readonly-probe",
                    source_snapshot={
                        "relative_path": source_path.name,
                        "path": source_path.absolute().as_posix(),
                        "size": snapshot.size,
                    },
                ),
            )
        )
        adapter = (factory or SiteAdapterFactory()).create(
            kind=kind,
            base_url=profile.base_url,
            credential_kind=profile.credential_kind,
            credential=entry["cookie"],
            timeout_seconds=15,
            user_agent="Mozilla/5.0",
        )
        page = await adapter.search(SearchQuery(keywords, SearchMediaType.MOVIE, page_size=20))
        # A media file may be replaced while a slow site is searching.
        # Do not fetch a torrent against stale source identity.
        if not _safe_file(source_path, data_root) or current_file_snapshot(source_path) != snapshot:
            return {"site": site, "status": "SOURCE_SNAPSHOT_CHANGED"}
        # This tool fetches at most one search page. Never call a candidate
        # unique if additional pages are explicitly advertised.
        if page.has_more or (page.total_hint is not None and page.total_hint > len(page.items)):
            return {
                "site": site,
                "status": "SEARCH_PAGE_INCOMPLETE",
                "returned_count": len(page.items),
            }
        candidates = tuple(
            item
            for item in page.items
            if item.site_id == kind.value.casefold()
            and item.total_size is not None
            and (item.seeders or 0) > 0
            and abs(item.total_size - snapshot.size) / max(snapshot.size, 1)
            <= _MAX_RELATIVE_SIZE_DIFF
            and candidate_search_relevant(unit.descriptor, item.descriptor)
            and not assess_unpack_candidate(unit.descriptor, item.descriptor).rejected
        )
        if len(candidates) != 1:
            return {
                "site": site,
                "status": "NO_SIZE_RELEVANT_CANDIDATE"
                if not candidates
                else "AMBIGUOUS_CANDIDATES",
                "returned_count": len(page.items),
                "viable_count": len(candidates),
            }
        if search_only:
            return {
                "site": site,
                "status": "UNIQUE_CANDIDATE",
                "returned_count": len(page.items),
                "viable_count": 1,
                "torrent_saved": False,
                "downloader_contacted": False,
            }
        await sleep(max(2.0, profile.search_interval_seconds))
        if not _safe_file(source_path, data_root) or current_file_snapshot(source_path) != snapshot:
            return {"site": site, "status": "SOURCE_SNAPSHOT_CHANGED"}
        stage = "FETCH_TORRENT"
        payload = await adapter.fetch_torrent(candidates[0].torrent_id)
        if (
            payload.site_id != kind.value.casefold()
            or payload.torrent_id != candidates[0].torrent_id
        ):
            return {"site": site, "status": "TORRENT_IDENTITY_MISMATCH"}
        stage = "VERIFY_FILES"
        meta = parse_torrent(payload.content)
        mappings = _map_existing_files(meta.files, source_path, data_root)
        mapped_count = sum(item.state is FileMappingState.MAPPED for item in mappings)
        needed = sum(not file.padding and not file.zero_length for file in meta.files)
        if mapped_count != needed:
            return {
                "site": site,
                "status": "FILE_MAPPING_INCOMPLETE",
                "mapped_count": mapped_count,
                "required_count": needed,
                "torrent_saved": False,
                "downloader_contacted": False,
            }
        verification: TorrentVerificationResult
        if meta.torrent_kind is TorrentKind.V1:
            verification = verify_v1_pieces(meta, mappings)
        elif meta.torrent_kind is TorrentKind.V2:
            verification = verify_v2_files(meta, mappings)
        else:
            verification = verify_hybrid(meta, mappings)
        verified, total = _verified_piece_counts(verification)
        if not _safe_file(source_path, data_root) or current_file_snapshot(source_path) != snapshot:
            return {"site": site, "status": "SOURCE_SNAPSHOT_CHANGED"}
        return {
            "site": site,
            "status": verification.level.value,
            "torrent_kind": meta.torrent_kind.value,
            "mapped_count": mapped_count,
            "required_count": needed,
            "pieces_verified": verified,
            "pieces_total": total,
            "torrent_saved": False,
            "downloader_contacted": False,
        }
    except Exception as exc:
        code = getattr(exc, "code", None)
        return {
            "site": site,
            "status": "FAILED_NO_RETRY",
            "stage": stage,
            "error_code": code if isinstance(code, str) and code.isidentifier() else "UNCLASSIFIED",
            "error_class": type(exc).__name__,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实影片辅种只读校验：不创建链接、不接触下载器")
    parser.add_argument("--site", choices=tuple(_COOKIE_SITES), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=_DEFAULT_DATA_ROOT)
    parser.add_argument("--secret-file", type=Path, default=_DEFAULT_SECRET_FILE)
    parser.add_argument("--query", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--acknowledge-download-record", action="store_true")
    args = parser.parse_args(argv)
    if not args.live or (not args.search_only and not args.acknowledge_download_record):
        print("未发起请求：需 --live；读取 torrent 还需 --acknowledge-download-record")
        return 2
    try:
        if (
            args.secret_file.is_symlink()
            or not args.secret_file.is_file()
            or stat.S_IMODE(args.secret_file.stat().st_mode) != 0o600
        ):
            print("凭据文件不存在、是符号链接或权限不是 0600；未发起请求")
            return 2
        loaded = json.loads(args.secret_file.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("非对象配置")
    except (OSError, ValueError, UnicodeError):
        print("凭据文件无法安全读取；未发起请求")
        return 2
    result = asyncio.run(
        check_movie_once(
            args.site,
            config=loaded,
            source_path=args.source,
            data_root=args.data_root,
            query_text=args.query,
            search_only=args.search_only,
        )
    )
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if result.get("status") in {"UNIQUE_CANDIDATE", VerificationLevel.FULL_VERIFIED.value}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
