"""Future release gates must exercise the published base image's Web updater."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_formal_transient_gate_starts_at_published_release_not_candidate() -> None:
    script = (ROOT / "scripts/check-updater-e2e.sh").read_text(encoding="utf-8")
    assert 'transient_start_image="$local_baseline_tag"' in script
    assert 'transient_start_version="$baseline_version"' in script
    assert 'test "$baseline_version" != "$candidate_version"' in script
    assert '    "$transient_start_image" >/dev/null' in script
    assert '--env PB_CURRENT_VERSION="$transient_start_version"' in script
    assert '--env PB_TARGET_VERSION="$candidate_version"' in script
    assert (
        'test "$transient_start_image_id" = "$(docker image inspect "$transient_start_image"'
        in script
    )
    assert '" = "$transient_start_version"' in script
    assert '" = "$candidate_version"' in script


def test_synthetic_arm64_mode_remains_distinct_from_cross_version_upgrade() -> None:
    script = (ROOT / "scripts/check-updater-e2e.sh").read_text(encoding="utf-8")
    assert 'if [[ "$baseline_mode" == formal ]]; then' in script
    assert 'transient_start_image="$local_candidate_tag"' in script
    assert 'baseline_version="$candidate_version"' in script
    assert "Synthetic ARM64 mode remains a same-version replacement" in script


def test_v103_requires_explicit_data_and_future_sources_test_anonymous_volume() -> None:
    script = (ROOT / "scripts/check-updater-e2e.sh").read_text(encoding="utf-8")
    assert 'if [[ "$baseline_mode" == formal && "$baseline_version" == 1.0.3 ]]; then' in script
    assert 'transient_mount_args=(--volume "$transient_data:/data")' in script
    assert '--volume "$transient_data/downloads:/data/downloads"' in script
    assert '--volume "$transient_data/downloads2:/data/downloads2"' in script
    assert 'transient_original_data_volume="$(docker inspect "$transient_main"' in script
    assert 'test -n "$transient_original_data_volume"' in script
    assert 'test "$(docker inspect "$transient_main"' in script
    assert ' = "$transient_original_data_volume"' in script
    assert 'Path("/data/.packbreaker-e2e-volume-probe").read_text()' in script
    assert 'Path("/data/downloads/.packbreaker-e2e-bind-probe").read_text()' in script
    assert 'Path("/data/downloads2/.packbreaker-e2e-bind-probe").read_text()' in script
