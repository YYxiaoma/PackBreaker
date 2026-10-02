from __future__ import annotations

from enum import StrEnum


class MovieDedupMode(StrEnum):
    AUTO = "AUTO"
    HARDLINK = "HARDLINK"
    SYMLINK = "SYMLINK"
    SCAN_ONLY = "SCAN_ONLY"


class MovieDedupCrossFilesystemPolicy(StrEnum):
    STOP = "STOP"
    SYMLINK = "SYMLINK"


class MovieDedupJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class MovieDedupJobPhase(StrEnum):
    PENDING = "PENDING"
    SCANNING_SOURCE = "SCANNING_SOURCE"
    SCANNING_TARGET = "SCANNING_TARGET"
    MATCHING = "MATCHING"
    VERIFYING = "VERIFYING"
    REVIEW = "REVIEW"
    EXECUTING = "EXECUTING"
    FINAL_VERIFYING = "FINAL_VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MovieDedupInventorySide(StrEnum):
    SOURCE = "A"
    TARGET = "B"


class MovieDedupInventoryStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    QUICK_HASHED = "QUICK_HASHED"
    FULL_HASHED = "FULL_HASHED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class MovieDedupPairStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VERIFIED_DUPLICATE = "VERIFIED_DUPLICATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ALREADY_DEDUPLICATED = "ALREADY_DEDUPLICATED"
    READY = "READY"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class MovieDedupResolvedAction(StrEnum):
    HARDLINK = "HARDLINK"
    SYMLINK = "SYMLINK"
    SCAN_ONLY = "SCAN_ONLY"
    BLOCKED = "BLOCKED"


class MovieDedupJournalStatus(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    TEMP_LINK_CREATED = "TEMP_LINK_CREATED"
    EXCHANGED = "EXCHANGED"
    VERIFIED = "VERIFIED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


def resolve_movie_dedup_action(
    mode: MovieDedupMode,
    cross_filesystem_policy: MovieDedupCrossFilesystemPolicy,
    *,
    same_filesystem: bool,
) -> MovieDedupResolvedAction:
    if mode is MovieDedupMode.SCAN_ONLY:
        return MovieDedupResolvedAction.SCAN_ONLY
    if mode is MovieDedupMode.SYMLINK:
        return MovieDedupResolvedAction.SYMLINK
    if same_filesystem:
        return MovieDedupResolvedAction.HARDLINK
    if (
        mode is MovieDedupMode.AUTO
        and cross_filesystem_policy is MovieDedupCrossFilesystemPolicy.SYMLINK
    ):
        return MovieDedupResolvedAction.SYMLINK
    return MovieDedupResolvedAction.BLOCKED
