from __future__ import annotations

import pytest

from scripts.verify_release_platforms import verify_release_index


def _index(*platforms: tuple[str, str]) -> dict[str, object]:
    return {"manifests": [{"platform": {"os": os, "architecture": arch}} for os, arch in platforms]}


def test_multiarch_index_accepts_only_two_real_platforms_and_provenance() -> None:
    verify_release_index(
        _index(("linux", "amd64"), ("unknown", "unknown"), ("linux", "arm64")),
        multiarch=True,
    )
    verify_release_index(_index(("linux", "amd64")), multiarch=False)
    arm_v8 = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ]
    }
    verify_release_index(arm_v8, multiarch=True)


@pytest.mark.parametrize(
    "index",
    [
        _index(("linux", "amd64")),
        _index(("linux", "arm64")),
        _index(("linux", "amd64"), ("linux", "amd64"), ("linux", "arm64")),
        _index(("linux", "amd64"), ("linux", "arm64"), ("linux", "arm/v7")),
        {"schemaVersion": 2},
        {"manifests": []},
    ],
)
def test_multiarch_index_rejects_missing_duplicate_or_extra_platforms(index: object) -> None:
    with pytest.raises(ValueError):
        verify_release_index(index, multiarch=True)
