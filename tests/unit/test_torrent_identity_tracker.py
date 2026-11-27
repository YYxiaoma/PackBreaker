from __future__ import annotations

from backend.app.domain.seeding_health import SeedingIdentitySource
from backend.app.domain.torrent_identity import (
    identity_from_comment,
    identity_from_tracker,
    identity_from_trackers,
)


def test_identity_from_tracker_prefers_announce_host() -> None:
    identity = identity_from_tracker("https://tracker.m-team.cc/announce?credential=c2lnbj0x&uid=1")

    assert identity is not None
    assert identity.site_key == "mteam"
    assert identity.torrent_id == ""
    assert identity.source is SeedingIdentitySource.TRACKER
    assert identity_from_tracker("https://example.invalid/announce") is None


def test_identity_from_trackers_skips_unrecognized_announce() -> None:
    identity = identity_from_trackers(
        [
            "https://public.example.invalid/announce",
            None,
            "https://tracker.hdtime.org/announce?passkey=abc",
        ]
    )

    assert identity is not None
    assert identity.site_key == "hdtime"
    assert identity.source is SeedingIdentitySource.TRACKER
    assert identity_from_trackers(["https://one.invalid/a", None]) is None
    assert identity_from_trackers([]) is None


def test_identity_from_comment_still_provides_remote_id() -> None:
    identity = identity_from_comment(
        "https://pt.keepfrds.com/details.php?id=4447&h=abc",
    )

    assert identity is not None
    assert identity.site_key == "keepfrds"
    assert identity.torrent_id == "4447"
    assert identity.source is SeedingIdentitySource.COMMENT
