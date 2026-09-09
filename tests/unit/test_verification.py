import pytest

from backend.app.domain.verification import (
    DownloaderKind,
    SkipCheckingReason,
    VerificationLevel,
    decide_skip_checking,
)


@pytest.mark.parametrize(
    ("level", "enabled", "current", "expected_reason"),
    [
        (
            VerificationLevel.CLIENT_CHECK_REQUIRED,
            True,
            True,
            SkipCheckingReason.FULL_VERIFICATION_REQUIRED,
        ),
        (
            VerificationLevel.BLOCKED,
            True,
            True,
            SkipCheckingReason.FULL_VERIFICATION_REQUIRED,
        ),
        (
            VerificationLevel.FULL_VERIFIED,
            False,
            True,
            SkipCheckingReason.EXPLICIT_ENABLE_REQUIRED,
        ),
        (
            VerificationLevel.FULL_VERIFIED,
            True,
            False,
            SkipCheckingReason.PREFLIGHT_STALE,
        ),
    ],
)
def test_qb_skip_checking_fails_closed(
    level: VerificationLevel,
    enabled: bool,
    current: bool,
    expected_reason: SkipCheckingReason,
) -> None:
    decision = decide_skip_checking(
        downloader=DownloaderKind.QBITTORRENT,
        verification_level=level,
        explicitly_enabled=enabled,
        preflight_current=current,
    )

    assert not decision.allowed
    assert decision.reason is expected_reason


def test_qb_full_verified_can_skip_only_when_explicitly_enabled() -> None:
    decision = decide_skip_checking(
        downloader=DownloaderKind.QBITTORRENT,
        verification_level=VerificationLevel.FULL_VERIFIED,
        explicitly_enabled=True,
        preflight_current=True,
    )

    assert decision.allowed
    assert decision.reason is SkipCheckingReason.ALLOWED


def test_transmission_never_skips_checking() -> None:
    decision = decide_skip_checking(
        downloader=DownloaderKind.TRANSMISSION,
        verification_level=VerificationLevel.FULL_VERIFIED,
        explicitly_enabled=True,
        preflight_current=True,
    )

    assert not decision.allowed
    assert decision.reason is SkipCheckingReason.DOWNLOADER_REQUIRES_CHECK
