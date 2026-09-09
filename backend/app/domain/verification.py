from dataclasses import dataclass
from enum import StrEnum


class VerificationLevel(StrEnum):
    FULL_VERIFIED = "FULL_VERIFIED"
    CLIENT_CHECK_REQUIRED = "CLIENT_CHECK_REQUIRED"
    BLOCKED = "BLOCKED"


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
