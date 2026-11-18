from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.infrastructure import docker_updater
from backend.app.infrastructure.docker_updater import DockerEngineClient, DockerUpdaterError


def _client(tmp_path: Path) -> DockerEngineClient:
    return DockerEngineClient(tmp_path / "docker.sock")


def test_pull_image_retries_transient_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """registry / 代理抖动导致的拉取失败要重试，不能直接判升级失败。"""

    client = _client(tmp_path)
    calls = {"count": 0}

    def flaky(image: str, *, timeout_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] < 3:
            raise DockerUpdaterError("DOCKER_IMAGE_PULL_FAILED", "目标镜像拉取失败")

    monkeypatch.setattr(client, "_pull_image_once", flaky)
    monkeypatch.setattr(docker_updater.time, "sleep", lambda _seconds: None)

    client.pull_image("ghcr.io/yyxiaoma/packbreaker@sha256:" + "a" * 64)

    assert calls["count"] == 3
    client.close()


def test_pull_image_gives_up_after_three_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    calls = {"count": 0}

    def always_fail(image: str, *, timeout_seconds: float) -> None:
        calls["count"] += 1
        raise DockerUpdaterError("DOCKER_IMAGE_PULL_FAILED", "目标镜像拉取失败")

    monkeypatch.setattr(client, "_pull_image_once", always_fail)
    monkeypatch.setattr(docker_updater.time, "sleep", lambda _seconds: None)

    with pytest.raises(DockerUpdaterError):
        client.pull_image("ghcr.io/yyxiaoma/packbreaker@sha256:" + "a" * 64)

    assert calls["count"] == 3
    client.close()
