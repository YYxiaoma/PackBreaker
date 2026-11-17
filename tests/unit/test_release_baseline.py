from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_release_baseline import load_release_baseline, project_version

ROOT = Path(__file__).resolve().parents[2]


def test_repository_release_baseline_is_immutable_v116_multiarch() -> None:
    baseline = load_release_baseline()

    assert baseline.version == "1.1.6"
    assert baseline.tag == "v1.1.6"
    assert baseline.commit == "3bdcd1fdf9ebb12eb72e035b48d8171e93696bb2"
    assert baseline.format_version == 2
    assert baseline.platform == "multi"
    assert baseline.platforms == ("linux/amd64", "linux/arm64")
    assert baseline.alembic_revision == "0056_movie_dedup_shared_link_bytes"
    assert baseline.release_workflow_run_id == 44057544025
    assert baseline.immutable_image == (
        "ghcr.io/yyxiaoma/packbreaker@"
        "sha256:9023686cc3810357f4feb6aea5077146a79ad5a313d34bad6b4e2153db51deb3"
    )


def test_v100_multiplatform_baseline_contract(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    payload.update(
        format_version=2,
        version="1.0.0",
        tag="v1.0.0",
        platform="multi",
        platforms=["linux/amd64", "linux/arm64"],
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    result = load_release_baseline(baseline, pyproject=pyproject)
    assert result.platforms == ("linux/amd64", "linux/arm64")
    assert result.as_dict()["platforms"] == ("linux/amd64", "linux/arm64")

    payload["platforms"] = ["linux/amd64"]
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="必须包含"):
        load_release_baseline(baseline, pyproject=pyproject)


def test_release_baseline_rejects_mutable_or_mismatched_identity(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    payload["image_digest"] = "sha256:short"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="完整 sha256"):
        load_release_baseline(baseline)

    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    payload["tag"] = "v0.1.2"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="tag 与 version"):
        load_release_baseline(baseline)


def test_release_baseline_cannot_be_newer_than_project_version(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    major, minor, patch = (int(part) for part in project_version().split("."))
    future_version = f"{major}.{minor}.{patch + 1}"
    payload["version"] = future_version
    payload["tag"] = f"v{future_version}"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="不能新于"):
        load_release_baseline(baseline)


def test_cross_image_gate_restores_baseline_backup_instead_of_downgrading_database() -> None:
    script = (ROOT / "scripts" / "check-release-upgrade.sh").read_text(encoding="utf-8")

    assert 'docker pull "$baseline_image"' in script
    assert "release_upgrade_probe" in script
    assert "backend.app.maintenance backup" in script
    assert "backend.app.maintenance restore-backup" in script
    assert "--confirm-replace-current-database" in script
    assert "alembic downgrade" not in script


def test_native_arm64_updater_gate_keeps_formal_amd64_baseline_unchanged() -> None:
    script = (ROOT / "scripts" / "check-updater-e2e.sh").read_text(encoding="utf-8")
    assert 'baseline_mode="${2:-formal}"' in script
    # 基线镜像走 pull 重试通道（ghcr 匿名 token 偶发 EOF）。
    assert 'docker_pull_with_retry "$baseline_image"' in script
    assert "docker_pull_with_retry()" in script
    assert 'if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then' in script
    assert 'baseline_version="$candidate_version"' in script
    assert 'test "$(uname -m)" = aarch64' in script
    assert 'test "$baseline_image_id" != "$candidate_image_id"' in script
    assert 'LABEL org.packbreaker.ci.synthetic-arm64-baseline="true"' in script


def test_updater_gate_resolves_digest_from_the_isolated_registry() -> None:
    """隔离 registry 的 digest 必须向 registry 索取。

    containerd 镜像存储会把「源 registry 的 index digest」写进本地 tag 的 RepoDigests，
    而隔离 registry 实际存放单平台 manifest。用前者去 pull 会 404，
    升级 helper 会报 DOCKER_IMAGE_PULL_FAILED（v1.1.6 发布实际踩到）。
    """

    script = (ROOT / "scripts" / "check-updater-e2e.sh").read_text(encoding="utf-8")

    assert "registry_digest_for_tag()" in script
    assert "docker-content-digest" in script.lower() or "docker-content-digest" in script
    # 不允许再按本地 RepoDigests 取 digest
    assert ".RepoDigests" not in script
    assert 'registry_digest_for_tag "$local_candidate_tag"' in script
    assert 'registry_digest_for_tag "$local_fault_tag"' in script


def test_updater_gate_compares_deployed_target_image_id_not_source_index() -> None:
    """升级后的容器要比对「实际部署的目标镜像 ID」，而不是源 index digest。"""

    script = (ROOT / "scripts" / "check-updater-e2e.sh").read_text(encoding="utf-8")

    assert 'candidate_target_image_id="$(docker image inspect "$candidate_target"' in script
    for container in ("success_main", "transient_main"):
        expected = (
            'test "$(docker inspect "$'
            + container
            + '" --format '
            + chr(39)
            + "{{.Image}}"
            + chr(39)
            + ')" = "$candidate_target_image_id"'
        )
        assert expected in script, container
