from dataclasses import dataclass
from enum import StrEnum


class VerificationLevel(StrEnum):
    FULL_VERIFIED = "FULL_VERIFIED"
    CLIENT_CHECK_REQUIRED = "CLIENT_CHECK_REQUIRED"
    BLOCKED = "BLOCKED"


class FileMappingState(StrEnum):
    MAPPED = "MAPPED"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"
    PADDING = "PADDING"
    ZERO_LENGTH = "ZERO_LENGTH"


class PieceStatus(StrEnum):
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int
    file_type: str = "regular"


@dataclass(frozen=True, slots=True)
class FileMappingEvidence:
    torrent_path: str
    state: FileMappingState
    source_path: str | None
    snapshot: FileSnapshot | None


@dataclass(frozen=True, slots=True)
class PieceEvidence:
    index: int
    status: PieceStatus
    covered_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class V1VerificationResult:
    level: VerificationLevel
    mappings: tuple[FileMappingEvidence, ...]
    pieces: tuple[PieceEvidence, ...]


@dataclass(frozen=True, slots=True)
class V2FileEvidence:
    torrent_path: str
    status: PieceStatus
    pieces: tuple[PieceEvidence, ...]


@dataclass(frozen=True, slots=True)
class V2VerificationResult:
    level: VerificationLevel
    mappings: tuple[FileMappingEvidence, ...]
    files: tuple[V2FileEvidence, ...]


@dataclass(frozen=True, slots=True)
class HybridVerificationResult:
    level: VerificationLevel
    v1: V1VerificationResult
    v2: V2VerificationResult


TorrentVerificationResult = V1VerificationResult | V2VerificationResult | HybridVerificationResult


class DownloaderKind(StrEnum):
    QBITTORRENT = "QBITTORRENT"
    TRANSMISSION = "TRANSMISSION"


class SkipCheckingReason(StrEnum):
    ALLOWED = "ALLOWED"
    DOWNLOADER_REQUIRES_CHECK = "DOWNLOADER_REQUIRES_CHECK"
    EXPLICIT_ENABLE_REQUIRED = "EXPLICIT_ENABLE_REQUIRED"
    PREFLIGHT_STALE = "PREFLIGHT_STALE"
    FULL_VERIFICATION_REQUIRED = "FULL_VERIFICATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class SkipCheckingDecision:
    allowed: bool
    reason: SkipCheckingReason


def decide_skip_checking(
    *,
    downloader: DownloaderKind,
    verification_level: VerificationLevel,
    explicitly_enabled: bool,
    preflight_current: bool,
) -> SkipCheckingDecision:
    """形成 qB 跳过校验的领域安全门；Transmission 永远不能通过。"""

    if downloader is not DownloaderKind.QBITTORRENT:
        return SkipCheckingDecision(False, SkipCheckingReason.DOWNLOADER_REQUIRES_CHECK)
    if not explicitly_enabled:
        return SkipCheckingDecision(False, SkipCheckingReason.EXPLICIT_ENABLE_REQUIRED)
    if not preflight_current:
        return SkipCheckingDecision(False, SkipCheckingReason.PREFLIGHT_STALE)
    if verification_level is not VerificationLevel.FULL_VERIFIED:
        return SkipCheckingDecision(False, SkipCheckingReason.FULL_VERIFICATION_REQUIRED)
    return SkipCheckingDecision(True, SkipCheckingReason.ALLOWED)
