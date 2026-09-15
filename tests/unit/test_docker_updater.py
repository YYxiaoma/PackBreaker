from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.app.infrastructure.backups import BackupArtifact, BackupError
from backend.app.infrastructure.docker_updater import (
    DockerUpdaterError,
    DockerUpgradeExecutor,
    build_replacement_plan,
)
from backend.app.infrastructure.updater_protocol import UpgradeHelperRequest

_OFFICIAL = "ghcr.io/yyxiaoma/packbreaker"
_TARGET = _OFFICIAL + "@sha256:" + "8" * 64


def _container() -> dict[str, Any]:
    return {
        "Id": "old-container-id",
        "Name": "/packbreaker",
        "Image": "sha256:" + "1" * 64,
        "Config": {
            "Image": f"{_OFFICIAL}:0.1.1",
            "User": "0:0",
            "Cmd": ["python", "-m", "backend.app.container_entrypoint"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Env": [
                "PATH=/opt/venv/bin:/usr/local/bin",
                "PYTHONUNBUFFERED=1",
                "PUID=1000",
                "PGID=1000",
                "PACKBREAKER_TIMEZONE=Asia/Shanghai",
            ],
            "Labels": {"custom.deploy": "standalone"},
            "ExposedPorts": {"8000/tcp": {}},
        },
        "HostConfig": {
            "AutoRemove": False,
            "Binds": [
                "/root/packbreaker/config:/config",
                "/srv/media:/data",
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
            "PortBindings": {"8000/tcp": [{"HostIp": "", "HostPort": "8000"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "bridge",
            "SecurityOpt": ["no-new-privileges"],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/root/packbreaker/config",
                "Destination": "/config",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/srv/media",
                "Destination": "/data",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            },
        ],
        "NetworkSettings": {
            "Networks": {
                "bridge": {
                    "Aliases": ["packbreaker"],
                    "IPAMConfig": None,
                    "MacAddress": "02:42:ac:11:00:0a",
                    "IPAddress": "172.17.0.10",
                    "Gateway": "172.17.0.1",
                }
            }
        },
    }


def _old_image() -> dict[str, Any]:
    return {
        "Config": {
            "User": "packbreaker",
            "Cmd": ["python", "-m", "backend.app.container_entrypoint"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Env": [
                "PATH=/opt/venv/bin:/usr/local/bin",
                "PYTHONUNBUFFERED=1",
            ],
            "Labels": {"org.opencontainers.image.title": "PackBreaker"},
            "ExposedPorts": {"8000/tcp": {}},
        }
    }


def test_replacement_plan_preserves_deployment_overrides_and_strips_docker_socket() -> None:
    plan = build_replacement_plan(
        _container(),
        _old_image(),
        target_image=_TARGET,
        allowed_image=_OFFICIAL,
    )

    assert plan.container_name == "packbreaker"
    assert plan.create_payload["Image"] == _TARGET
    assert plan.create_payload["User"] == "0:0"
    assert set(plan.create_payload["Env"]) == {
        "PUID=1000",
        "PGID=1000",
        "PACKBREAKER_TIMEZONE=Asia/Shanghai",
    }
    host = plan.create_payload["HostConfig"]
    assert host["RestartPolicy"]["Name"] == "unless-stopped"
    assert host["PortBindings"]["8000/tcp"][0]["HostPort"] == "8000"
    assert host["NetworkMode"] == "bridge"
    assert plan.create_payload["NetworkingConfig"] == {
        "EndpointsConfig": {"bridge": {"Aliases": ["packbreaker"]}}
    }
    assert "/root/packbreaker/config:/config" in host["Binds"]
    assert "/srv/media:/data" in host["Binds"]
    assert all("docker.sock" not in item for item in host["Binds"])
    assert plan.config_bind == "/root/packbreaker/config:/config"


def test_replacement_plan_accepts_private_registry_with_port_for_isolated_e2e() -> None:
    repository = "127.0.0.1:5000/packbreaker"
    container = _container()
    container["Config"]["Image"] = f"{repository}:baseline"
    target = repository + "@sha256:" + "8" * 64

    plan = build_replacement_plan(
        container,
        _old_image(),
        target_image=target,
        allowed_image=repository,
    )

    assert plan.create_payload["Image"] == target


def test_replacement_plan_rejects_invalid_private_registry_port() -> None:
    repository = "127.0.0.1:99999/packbreaker"
    container = _container()
    container["Config"]["Image"] = f"{repository}:baseline"
    target = repository + "@sha256:" + "8" * 64

    with pytest.raises(DockerUpdaterError) as exc_info:
        build_replacement_plan(
            container,
            _old_image(),
            target_image=target,
            allowed_image=repository,
        )

    assert exc_info.value.code == "UPGRADE_TARGET_IMAGE_UNTRUSTED"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda item: item["Config"]["Labels"].__setitem__(
                "com.docker.compose.project", "packbreaker"
            ),
            "UPGRADE_COMPOSE_MANAGED_UNSUPPORTED",
        ),
        (
            lambda item: item["NetworkSettings"]["Networks"]["bridge"].__setitem__(
                "IPAMConfig", {"IPv4Address": "172.17.0.10"}
            ),
            "UPGRADE_STATIC_NETWORK_ADDRESS_UNSUPPORTED",
        ),
        (
            lambda item: item["Config"].__setitem__("MacAddress", "02:42:ac:11:00:0a"),
            "UPGRADE_STATIC_MAC_ADDRESS_UNSUPPORTED",
        ),
        (
            lambda item: item["HostConfig"].__setitem__("AutoRemove", True),
            "UPGRADE_AUTOREMOVE_UNSUPPORTED",
        ),
        (
            lambda item: item["HostConfig"].__setitem__("NetworkMode", "container:other"),
            "UPGRADE_NETWORK_MODE_UNSUPPORTED",
        ),
        (
            lambda item: item["NetworkSettings"].__setitem__("Networks", {"one": {}, "two": {}}),
            "UPGRADE_MULTI_NETWORK_UNSUPPORTED",
        ),
    ],
)
def test_replacement_plan_fails_closed_for_unsafe_runtime_shapes(
    mutation: Any,
    code: str,
) -> None:
    container = _container()
    mutation(container)
    with pytest.raises(DockerUpdaterError) as exc_info:
        build_replacement_plan(
            container, _old_image(), target_image=_TARGET, allowed_image=_OFFICIAL
        )
    assert exc_info.value.code == code


class FakeDocker:
    def __init__(self, *, fail_new_health: bool = False) -> None:
        self.fail_new_health = fail_new_health
        self.calls: list[tuple[str, object]] = []

    def inspect_container(self, container: str) -> dict[str, Any]:
        self.calls.append(("inspect_container", container))
        return _container()

    def inspect_image(self, image: str) -> dict[str, Any]:
        self.calls.append(("inspect_image", image))
        return _old_image()

    def pull_image(self, image: str, *, timeout_seconds: float = 900.0) -> None:
        self.calls.append(("pull", image))

    def stop_container(self, container: str, *, timeout_seconds: int = 30) -> None:
        self.calls.append(("stop", container))

    def rename_container(self, container: str, new_name: str) -> None:
        self.calls.append(("rename", (container, new_name)))

    def create_container(self, name: str, payload: Mapping[str, Any]) -> str:
        self.calls.append(("create", (name, payload["Image"])))
        return "new-container-id"

    def start_container(self, container: str) -> None:
        self.calls.append(("start", container))

    def remove_container(self, container: str, *, force: bool = False) -> None:
        self.calls.append(("remove", (container, force)))

    def wait_healthy(
        self,
        container: str,
        *,
        timeout_seconds: float = 180.0,
        poll_seconds: float = 2.0,
    ) -> None:
        self.calls.append(("healthy", container))
        if container == "new-container-id" and self.fail_new_health:
            raise DockerUpdaterError("DOCKER_NEW_CONTAINER_UNHEALTHY", "synthetic failure")

    def run_restore_container(
        self,
        *,
        image_id: str,
        config_bind: str,
        database_file: str,
        manifest_file: str,
        name: str,
    ) -> None:
        self.calls.append(("restore", (image_id, config_bind, database_file, manifest_file, name)))


def _request() -> UpgradeHelperRequest:
    return UpgradeHelperRequest(
        request_id="request-e2e-001",
        current_version="0.1.1",
        target_version="0.1.2",
        target_image=_TARGET,
        backup_database_file="packbreaker-20260915T010101Z-1234abcd.db",
        backup_manifest_file="packbreaker-20260915T010101Z-1234abcd.json",
        grace_seconds=0.5,
    )


def _backup_factory(database_path: Path, backup_dir: Path, *, app_version: str) -> BackupArtifact:
    return BackupArtifact(
        database_path=backup_dir / "packbreaker-20260915T020202Z-aabbccdd.db",
        manifest_path=backup_dir / "packbreaker-20260915T020202Z-aabbccdd.json",
        created_at=datetime(2026, 9, 15, tzinfo=UTC),
        database_sha256="0" * 64,
        database_size_bytes=1024,
        alembic_revision="0023_backup_policy",
        app_version=app_version,
    )


def test_upgrade_executor_switches_to_new_container_after_health() -> None:
    docker = FakeDocker()
    phases: list[str] = []
    outcome = DockerUpgradeExecutor(
        docker,
        target_container="packbreaker",
        allowed_image=_OFFICIAL,
        config_dir=Path("/config"),
        backup_factory=_backup_factory,
    ).execute(_request(), phase=lambda phase, _message: phases.append(phase))

    assert outcome.phase == "succeeded"
    assert outcome.rollback_performed is False
    assert phases == ["pulling", "stopping", "starting", "verifying"]
    assert ("remove", ("old-container-id", False)) in docker.calls
    assert not any(call[0] == "restore" for call in docker.calls)


def test_upgrade_executor_restores_quiesced_database_and_old_container_on_health_failure() -> None:
    docker = FakeDocker(fail_new_health=True)
    outcome = DockerUpgradeExecutor(
        docker,
        target_container="packbreaker",
        allowed_image=_OFFICIAL,
        config_dir=Path("/config"),
        backup_factory=_backup_factory,
    ).execute(_request(), phase=lambda _phase, _message: None)

    assert outcome.phase == "rolled_back"
    assert outcome.rollback_performed is True
    restore_index = next(index for index, call in enumerate(docker.calls) if call[0] == "restore")
    restart_index = docker.calls.index(("start", "old-container-id"))
    assert restore_index < restart_index
    assert ("healthy", "old-container-id") in docker.calls


def test_upgrade_executor_restarts_old_if_quiesced_backup_fails() -> None:
    docker = FakeDocker()

    def fail_backup(database_path: Path, backup_dir: Path, *, app_version: str) -> BackupArtifact:
        raise BackupError("synthetic backup failure")

    outcome = DockerUpgradeExecutor(
        docker,
        target_container="packbreaker",
        allowed_image=_OFFICIAL,
        config_dir=Path("/config"),
        backup_factory=fail_backup,
    ).execute(_request(), phase=lambda _phase, _message: None)

    assert outcome.phase == "rolled_back"
    assert not any(call[0] == "restore" for call in docker.calls)
    assert ("start", "old-container-id") in docker.calls
    assert not any(call[0] == "create" for call in docker.calls)
