from __future__ import annotations

from dataclasses import dataclass

from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway


@dataclass(frozen=True, slots=True)
class ExistingReuseProof:
    target_snapshot: FileSnapshot
    sha256: str | None


def verify_existing_reuse(
    filesystem: SafeFilesystemGateway,
    *,
    source_path: str,
    source_snapshot: FileSnapshot,
    target_path: str,
    expected_target: FileSnapshot | None = None,
    expected_sha256: str | None = None,
) -> ExistingReuseProof:
    """Read-only, no-follow evidence that an existing target equals the frozen source.

    The same inode is sufficient for hardlink reuse. Distinct inodes require
    complete SHA-256 reads from safe file descriptors. Snapshots are checked
    before and after hashing. Neither file is ever opened for writing.
    """
    filesystem.assert_source_matches(
        source_relative_path=source_path, expected_source_snapshot=source_snapshot
    )
    source = filesystem.inspect_movie_file(relative_path=source_path)
    target = filesystem.inspect_movie_file(relative_path=target_path)
    frozen = target.filesystem_snapshot()
    result = FileSnapshot(
        device=frozen.device,
        inode=frozen.inode,
        size=frozen.size,
        mtime_ns=frozen.mtime_ns,
        file_type=frozen.file_type,
    )
    if source.size != source_snapshot.size or result.size != source_snapshot.size:
        raise ValueError("复用目标文件大小与验证证据不一致")
    if expected_target is not None and result != expected_target:
        raise ValueError("复用目标文件身份或快照已变化")

    same_inode = source.device == target.device and source.inode == target.inode
    if same_inode:
        if expected_sha256 is not None:
            raise ValueError("复用计划的同 inode 证明类型发生变化")
        return ExistingReuseProof(result, None)
    source_hash = filesystem.movie_full_sha256(relative_path=source_path, expected_snapshot=source)
    target_hash = filesystem.movie_full_sha256(relative_path=target_path, expected_snapshot=target)
    if source_hash != target_hash:
        raise ValueError("复用目标内容与原验证源文件不一致")
    if expected_target is not None and expected_sha256 != source_hash:
        raise ValueError("复用目标内容指纹与冻结计划不一致")
    return ExistingReuseProof(result, source_hash)
