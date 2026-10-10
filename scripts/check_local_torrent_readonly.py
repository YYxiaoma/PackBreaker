"""Offline, user-provided .torrent content evidence. Never contact trackers or downloaders."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from backend.app.domain.errors import DomainViolation
from backend.app.domain.torrent import TorrentKind
from backend.app.domain.verification import (
    FileMappingState,
    HybridVerificationResult,
    V1VerificationResult,
    V2VerificationResult,
)
from backend.app.infrastructure.piece_verifier import (
    verify_hybrid,
    verify_v1_pieces,
    verify_v2_files,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot
from backend.app.infrastructure.torrent_parser import BencodeLimits, parse_torrent
from scripts.check_movie_crossseed_readonly import (
    _map_existing_files,
    _safe_file,
    _verified_piece_counts,
)

_MAX_BYTES = BencodeLimits().max_payload_bytes


def _read_user_torrent(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o077
            or not 0 < before.st_size <= _MAX_BYTES
        ):
            return None
        with os.fdopen(os.dup(fd), "rb") as stream:
            payload = stream.read(_MAX_BYTES + 1)
        after = os.fstat(fd)
        if len(payload) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return None
        return payload
    except OSError:
        return None
    finally:
        os.close(fd)


def check_local_torrent(*, torrent_file: Path, source: Path, data_root: Path) -> dict[str, object]:
    """Verify every Piece against authorized source media, with no side effects."""
    report: dict[str, object] = {
        "status": "BLOCKED",
        "offline_only": True,
        "tracker_contacted": False,
        "downloader_contacted": False,
        "approved_for_client_add": False,
    }
    if (
        not data_root.is_dir()
        or not _safe_file(source, data_root)
        or source.suffix.casefold() not in {".mkv", ".mp4", ".avi", ".ts"}
    ):
        report["status"] = "SOURCE_BLOCKED"
        return report
    payload = _read_user_torrent(torrent_file)
    if payload is None:
        report["status"] = "TORRENT_FILE_BLOCKED"
        return report
    try:
        snapshot = current_file_snapshot(source)
        meta = parse_torrent(payload)
        mapping = _map_existing_files(meta.files, source, data_root)
        required = sum(not f.padding and not f.zero_length for f in meta.files)
        mapped = sum(m.state is FileMappingState.MAPPED for m in mapping)
        report.update(
            torrent_kind=meta.torrent_kind.value,
            file_count=len(meta.files),
            required_files=required,
            mapped_files=mapped,
        )
        if mapped != required:
            report["status"] = "FILE_MAPPING_INCOMPLETE"
            return report
        result: V1VerificationResult | V2VerificationResult | HybridVerificationResult
        if meta.torrent_kind is TorrentKind.V1:
            result = verify_v1_pieces(meta, mapping)
        elif meta.torrent_kind is TorrentKind.V2:
            result = verify_v2_files(meta, mapping)
        else:
            result = verify_hybrid(meta, mapping)
        if not _safe_file(source, data_root) or current_file_snapshot(source) != snapshot:
            report["status"] = "SOURCE_SNAPSHOT_CHANGED"
            return report
        count, total = _verified_piece_counts(result)
        report.update(status=result.level.value, pieces_verified=count, pieces_total=total)
    except (DomainViolation, ValueError, OSError):
        report["status"] = "VERIFICATION_FAILED"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地私种离线完整 Piece 验收")
    parser.add_argument("--torrent-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = check_local_torrent(
        torrent_file=args.torrent_file, source=args.source, data_root=args.data_root
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "FULL_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
