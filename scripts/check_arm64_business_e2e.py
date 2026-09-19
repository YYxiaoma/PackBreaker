"""Isolated ARM64 container probe: real filesystem and v1/v2/hybrid full-piece verification.

CI pipes this source into the running candidate container with `docker exec -i python -`.
Only a newly created temporary directory below the supplied CI-only data root is used.
This does not contact PT sites or a real downloader, or authorize a seeding task.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from backend.app.application.m2_corpus_acceptance import run_m2_corpus_acceptance
from backend.app.domain.verification import VerificationLevel


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        entries = sorted(value.items(), key=lambda item: item[0])
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in entries) + b"e"
    raise TypeError(type(value).__name__)


def _torrent(kind: str, content: bytes) -> bytes:
    piece_length = 16 * 1024
    info: dict[bytes, object] = {
        b"name": b"Pack" if kind != "v1" else b"movie.mkv",
        b"piece length": piece_length,
    }
    if kind in {"v2", "hybrid"}:
        info.update(
            {
                b"meta version": 2,
                b"file tree": {
                    b"movie.mkv": {
                        b"": {
                            b"length": len(content),
                            b"pieces root": hashlib.sha256(content).digest(),
                        }
                    }
                },
            }
        )
    if kind == "v1":
        info[b"length"] = len(content)
    elif kind == "hybrid":
        info[b"files"] = [{b"length": len(content), b"path": [b"movie.mkv"]}]
    if kind in {"v1", "hybrid"}:
        info[b"pieces"] = hashlib.sha1(content).digest()
    return _bencode({b"announce": b"https://example.invalid/announce", b"info": info})


def run_business_probe(root: Path) -> dict[str, str]:
    """Never enter an existing source directory; create and remove only our own fixture."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("ARM64 CI 数据根目录必须是已存在的非符号链接目录")
    outcomes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="packbreaker-arm64-business-", dir=root) as temp:
        base = Path(temp)
        content = b"synthetic-movie-bytes-for-arm64-validation"
        for kind in ("v1", "v2", "hybrid"):
            case = base / kind
            source_root = case / "source"
            source_root.mkdir(parents=True)
            media = source_root / "movie.mkv"
            media.write_bytes(content)
            torrent = case / "synthetic.torrent"
            torrent.write_bytes(_torrent(kind, content))
            before = media.stat(follow_symlinks=False)
            verified = run_m2_corpus_acceptance(
                torrent_path=torrent,
                source_root=source_root,
                repeated_runs=3,
                verify_content=True,
            )
            after = media.stat(follow_symlinks=False)
            assert verified.status == "PASS", kind
            assert verified.verification_level is VerificationLevel.FULL_VERIFIED, kind
            assert verified.inventory_stable and verified.mapping_stable, kind
            assert verified.source_unchanged_after_verification is True, kind
            assert verified.execution_allowed is False and verified.side_effects_started is False
            assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )

            # Real same-device hardlink; never write through its shared inode.
            target = case / "target" / "movie.mkv"
            target.parent.mkdir()
            os.link(media, target)
            assert (target.stat().st_dev, target.stat().st_ino) == (before.st_dev, before.st_ino)
            assert target.read_bytes() == content
            target.unlink()
            assert media.read_bytes() == content

            # Negative evidence: a corrupt synthetic source can no longer be FULL_VERIFIED.
            media.write_bytes(b"X" + content[1:])
            rejected = run_m2_corpus_acceptance(
                torrent_path=torrent,
                source_root=source_root,
                repeated_runs=2,
                verify_content=True,
            )
            assert rejected.verification_level is not VerificationLevel.FULL_VERIFIED, kind
            assert rejected.execution_allowed is False and rejected.side_effects_started is False
            outcomes[kind] = "FULL_VERIFIED; hardlink=ok; corrupt=not_full_verified"
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description="只在隔离 CI 数据卷生成并清理合成媒体验证数据")
    parser.add_argument("--data-root", type=Path, default=Path("/data"))
    args = parser.parse_args()
    print(json.dumps(run_business_probe(args.data_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
