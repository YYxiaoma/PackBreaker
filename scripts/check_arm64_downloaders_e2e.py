"""Real isolated ARM64 downloader protocol probe; all media is synthetic.

Executed inside the PackBreaker candidate image sharing the isolated downloader
container's network namespace. The downloader itself uses Docker network=none.
Never use this probe with a production downloader or a real media mount.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from backend.app.domain.downloader import DownloaderCredential
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    QbittorrentAdapter,
    QbittorrentAddRequest,
    QbittorrentTorrentState,
    TransmissionAdapter,
    TransmissionAddRequest,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

_ACTIVE_STAGE = "setup"


def _mark_stage(stage: str) -> None:
    global _ACTIVE_STAGE
    _ACTIVE_STAGE = stage


def _assert_qb_state(
    states: tuple[QbittorrentTorrentState, ...],
    *,
    save_path: str,
    tag: str,
) -> None:
    """Emit only synthetic test state, never credentials or arbitrary API response bodies."""
    if (
        len(states) == 1
        and states[0].verification_complete
        and states[0].save_path == save_path
        and tag in states[0].tags
    ):
        return
    state = states[0] if len(states) == 1 else None
    print(
        "::error file=scripts/check_arm64_downloaders_e2e.py::"
        f"qb synthetic status count={len(states)} "
        f"state={state.state if state else 'missing-or-duplicate'} "
        f"progress={state.progress if state else 'unknown'} "
        f"verification_complete={state.verification_complete if state else False} "
        f"save_path_matches={state.save_path == save_path if state else False} "
        f"tag_present={tag in state.tags if state else False}",
        flush=True,
    )
    raise AssertionError(
        "qBittorrent synthetic torrent state is not verified or has identity drift"
    )


def synthetic_torrent(content: bytes) -> bytes:
    piece_length = 16 * 1024
    pieces = b"".join(
        hashlib.sha1(content[i : i + piece_length]).digest()  # noqa: S324 - BitTorrent v1
        for i in range(0, len(content), piece_length)
    )
    info = (
        b"d6:lengthi"
        + str(len(content)).encode()
        + b"e4:name9:movie.mkv"
        + b"12:piece lengthi16384e6:pieces"
        + str(len(pieces)).encode()
        + b":"
        + pieces
        + b"7:privatei1ee"
    )
    return b"d8:announce32:https://example.invalid/announce4:info" + info + b"e"


async def run_real_downloader_probe(kind: str, data_root: Path, password: str) -> None:
    if kind not in {"qbittorrent", "transmission"}:
        raise ValueError("未知的隔离测试下载器")
    if not data_root.is_dir() or data_root.is_symlink():
        raise ValueError("仅允许已创建的独立非符号链接测试数据根目录")
    if not password:
        raise ValueError("缺少隔离下载器一次性测试密码")
    content = b"PACKBREAKER-ISOLATED-ARM64-TEST" * 1000
    with tempfile.TemporaryDirectory(prefix="packbreaker-real-downloader-", dir=data_root) as tmp:
        root = Path(tmp)
        source = root / "source" / "movie.mkv"
        source.parent.mkdir()
        source.write_bytes(content)
        target = root / "target" / "movie.mkv"
        target.parent.mkdir()
        os.link(source, target)
        before = source.stat(follow_symlinks=False)
        assert before.st_ino == target.stat(follow_symlinks=False).st_ino
        torrent = synthetic_torrent(content)
        meta = parse_torrent(torrent)
        assert meta.v1_info_hash is not None
        torrent_hash = meta.v1_info_hash
        if kind == "qbittorrent":
            _mark_stage("qb-connect")
            qb_adapter = QbittorrentAdapter(
                "http://127.0.0.1:8080",
                DownloaderCredential(username="admin", password=password),
            )
            capabilities = (await qb_adapter.test_connection()).capabilities
            assert capabilities.version.lstrip("v").startswith("5.2.3"), capabilities.version
            assert capabilities.supports_skip_checking
            _mark_stage("qb-add")
            qb_added = await qb_adapter.add_torrent(
                QbittorrentAddRequest(
                    torrent_content=torrent,
                    save_path=str(target.parent),
                    verification_level=VerificationLevel.FULL_VERIFIED,
                    skip_checking=True,
                    tags=("packbreaker-arm64-ci",),
                )
            )
            assert qb_added.success_count == 1 and qb_added.failure_count == 0, qb_added
            assert torrent_hash in qb_added.added_torrent_ids, qb_added
            _mark_stage("qb-verify-status")
            for _ in range(45):
                qb_states = await qb_adapter.get_torrents((torrent_hash,))
                if qb_states and qb_states[0].verification_complete:
                    break
                await asyncio.sleep(1)
            _assert_qb_state(qb_states, save_path=str(target.parent), tag="packbreaker-arm64-ci")
            _mark_stage("qb-start")
            await qb_adapter.start_torrent(torrent_hash)
            _mark_stage("qb-seeding-status")
            for _ in range(30):
                qb_states = await qb_adapter.get_torrents((torrent_hash,))
                if qb_states and qb_states[0].seeding:
                    break
                await asyncio.sleep(1)
            assert len(qb_states) == 1 and qb_states[0].seeding, qb_states
            _mark_stage("qb-stop-remove")
            await qb_adapter.stop_torrent(torrent_hash)
            await qb_adapter.remove_torrent_keep_files(torrent_hash)
            assert not await qb_adapter.get_torrents((torrent_hash,))
        else:
            _mark_stage("tr-connect")
            tr_adapter = TransmissionAdapter(
                "http://127.0.0.1:9091/transmission/rpc",
                DownloaderCredential(username="packbreaker", password=password),
            )
            capabilities = (await tr_adapter.test_connection()).capabilities
            assert capabilities.version.startswith("4.1.3"), capabilities.version
            assert not capabilities.supports_skip_checking
            _mark_stage("tr-add")
            tr_added = await tr_adapter.add_torrent(
                TransmissionAddRequest(
                    torrent_content=torrent,
                    save_path=str(target.parent),
                    labels=("packbreaker-arm64-ci",),
                )
            )
            assert not tr_added.duplicate and tr_added.torrent_hash == torrent_hash
            tr_states = await tr_adapter.get_torrents((torrent_hash,))
            assert (
                len(tr_states) == 1
                and tr_states[0].stopped
                and tr_states[0].download_dir == str(target.parent)
            )
            _mark_stage("tr-verify")
            await tr_adapter.verify_torrent(torrent_hash)
            for _ in range(45):
                tr_states = await tr_adapter.get_torrents((torrent_hash,))
                if tr_states and tr_states[0].verification_complete:
                    break
                await asyncio.sleep(1)
            assert len(tr_states) == 1 and tr_states[0].verification_complete, tr_states
            _mark_stage("tr-start")
            await tr_adapter.start_torrent(torrent_hash)
            _mark_stage("tr-seeding-status")
            for _ in range(30):
                tr_states = await tr_adapter.get_torrents((torrent_hash,))
                if tr_states and tr_states[0].seeding:
                    break
                await asyncio.sleep(1)
            assert len(tr_states) == 1 and tr_states[0].seeding, tr_states
            _mark_stage("tr-stop-remove")
            await tr_adapter.stop_torrent(torrent_hash)
            await tr_adapter.remove_torrent_keep_files(torrent_hash)
            assert not await tr_adapter.get_torrents((torrent_hash,))
        after = source.stat(follow_symlinks=False)
        assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        assert target.read_bytes() == source.read_bytes() == content
        assert target.stat(follow_symlinks=False).st_ino == before.st_ino


def main() -> None:
    parser = argparse.ArgumentParser(description="隔离 ARM64 真下载器协议验收")
    parser.add_argument("kind", choices=("qbittorrent", "transmission"))
    parser.add_argument("--data-root", type=Path, default=Path("/downloads"))
    args = parser.parse_args()
    password = os.environ.get("PACKBREAKER_CI_DOWNLOADER_PASSWORD", "")
    try:
        asyncio.run(run_real_downloader_probe(args.kind, args.data_root, password))
    except Exception as exc:
        # CI annotations must not include third-party response bodies or credentials.
        code = getattr(exc, "code", None)
        safe_code = code if isinstance(code, str) and code.isidentifier() else "unspecified"
        print(
            f"::error file=scripts/check_arm64_downloaders_e2e.py::"
            f"isolated {args.kind} API phase={_ACTIVE_STAGE} "
            f"exception={type(exc).__name__} code={safe_code}",
            flush=True,
        )
        raise
    print(f"isolated {args.kind} real API E2E passed")


if __name__ == "__main__":
    main()
