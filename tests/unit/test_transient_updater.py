from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import backend.app.infrastructure.transient_updater as transient_module
from backend.app.infrastructure.transient_updater import TransientUpdaterLauncher
from backend.app.infrastructure.updater_protocol import (
    UPDATER_PROTOCOL_VERSION,
    UpdaterProtocolError,
    UpdaterStatus,
    UpgradeHelperRequest,
)

_OFFICIAL = "ghcr.io/yyxiaoma/packbreaker"
_TARGET = _OFFICIAL + "@sha256:" + "8" * 64


def _container(config_dir: Path, docker_socket: Path) -> dict[str, Any]:
    return {
        "Id": "main-container",
        "Name": "/packbreaker",
        "Image": "sha256:" + "1" * 64,
        "Config": {
            "Image": f"{_OFFICIAL}:0.1.4",
            "User": "0:0",
            "Cmd": ["python", "-m", "backend.app.container_entrypoint"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Env": ["PUID=0", "PGID=0"],
            "Labels": {},
            "ExposedPorts": {"8000/tcp": {}},
        },
        "HostConfig": {
            "AutoRemove": False,
            "Binds": [
                f"{config_dir}:/config",
                f"{docker_socket}:/var/run/docker.sock",
            ],
            "PortBindings": {"8000/tcp": [{"HostIp": "", "HostPort": "8000"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "bridge",
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(config_dir),
                "Destination": "/config",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": str(docker_socket),
                "Destination": "/var/run/docker.sock",
                "RW": True,
            },
        ],
        "NetworkSettings": {
            "Networks": {"bridge": {"Aliases": ["packbreaker"], "IPAMConfig": None}}
        },
    }


def _image() -> dict[str, Any]:
    return {
        "Config": {
            "User": "packbreaker",
            "Cmd": ["python", "-m", "backend.app.container_entrypoint"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Env": [],
            "Labels": {},
            "ExposedPorts": {"8000/tcp": {}},
        }
    }


def _request() -> UpgradeHelperRequest:
    return UpgradeHelperRequest(
        request_id="pb-upgrade-test-001",
        current_version="0.1.4",
        target_version="0.1.5",
        target_image=_TARGET,
        backup_database_file="packbreaker-20260916T010101Z-1234abcd.db",
        backup_manifest_file="packbreaker-20260916T010101Z-1234abcd.json",
    )


class FakeDockerEngineClient:
    instances: list[FakeDockerEngineClient] = []
    config_dir: Path
    docker_socket: Path
    helper_available = True

    def __init__(self, docker_socket: Path) -> None:
        self.socket = docker_socket
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        type(self).instances.append(self)

    def __enter__(self) -> FakeDockerEngineClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass

    def inspect_container(self, container: str) -> dict[str, Any]:
        if container.startswith("packbreaker-updater-once-"):
            if not type(self).helper_available:
                from backend.app.infrastructure.docker_updater import DockerUpdaterError

                raise DockerUpdaterError(
                    "DOCKER_CONTAINER_INSPECT_FAILED", "synthetic missing helper"
                )
            return {"Id": "oneshot-helper-id"}
        assert container == "packbreaker"
        return _container(type(self).config_dir, type(self).docker_socket)

    def inspect_image(self, image: str) -> dict[str, Any]:
        assert image.startswith("sha256:")
        return _image()

    def create_container(self, name: str, payload: dict[str, Any]) -> str:
        self.created.append((name, payload))
        return "oneshot-helper-id"

    def start_container(self, container: str) -> None:
        self.started.append(container)

    def remove_container(self, container: str, *, force: bool = False) -> None:
        self.removed.append(container)


def test_transient_launcher_starts_auto_remove_helper_and_persists_accepted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    docker_socket = tmp_path / "docker.sock"
    docker_socket.touch()
    FakeDockerEngineClient.instances = []
    FakeDockerEngineClient.helper_available = True
    FakeDockerEngineClient.config_dir = config_dir
    FakeDockerEngineClient.docker_socket = docker_socket
    monkeypatch.setattr(transient_module, "DockerEngineClient", FakeDockerEngineClient)
    launcher = TransientUpdaterLauncher(
        config_dir=config_dir,
        docker_socket=docker_socket,
        target_container="packbreaker",
        allowed_image=_OFFICIAL,
    )

    assert launcher.status().phase == "idle"
    accepted = launcher.start_upgrade(_request())

    assert accepted.phase == "accepted"
    assert accepted.backup_database_file == "packbreaker-20260916T010101Z-1234abcd.db"
    persisted = launcher.status()
    assert persisted.request_id == "pb-upgrade-test-001"
    assert persisted.backup_database_file == "packbreaker-20260916T010101Z-1234abcd.db"
    create_calls = [item for client in FakeDockerEngineClient.instances for item in client.created]
    assert len(create_calls) == 1
    helper_name, payload = create_calls[0]
    assert helper_name.startswith("packbreaker-updater-once-")
    assert payload["User"] == "0:0"
    assert payload["HostConfig"]["AutoRemove"] is True
    assert payload["HostConfig"]["NetworkMode"] == "none"
    assert f"{docker_socket}:/var/run/docker.sock" in payload["HostConfig"]["Binds"]
    assert payload["WorkingDir"] == "/app"
    assert payload["Entrypoint"] == [
        "/opt/venv/bin/python",
        "-m",
        "backend.app.updater_helper",
    ]
    request_files = list((config_dir / "transient-updater" / "requests").glob("*.json"))
    assert len(request_files) == 1

    replayed = launcher.start_upgrade(_request())
    assert replayed.request_id == accepted.request_id
    create_calls_after = [
        item for client in FakeDockerEngineClient.instances for item in client.created
    ]
    assert len(create_calls_after) == 1


def test_transient_launcher_requires_mounted_docker_socket(tmp_path: Path) -> None:
    launcher = TransientUpdaterLauncher(
        config_dir=tmp_path,
        docker_socket=tmp_path / "missing.sock",
    )

    with pytest.raises(UpdaterProtocolError) as exc_info:
        launcher.status()

    assert exc_info.value.code == "UPDATER_DOCKER_SOCKET_REQUIRED"


def test_transient_launcher_marks_missing_helper_during_switch_for_manual_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    docker_socket = tmp_path / "docker.sock"
    docker_socket.touch()
    FakeDockerEngineClient.instances = []
    FakeDockerEngineClient.config_dir = config_dir
    FakeDockerEngineClient.docker_socket = docker_socket
    FakeDockerEngineClient.helper_available = False
    monkeypatch.setattr(transient_module, "DockerEngineClient", FakeDockerEngineClient)
    launcher = TransientUpdaterLauncher(config_dir=config_dir, docker_socket=docker_socket)
    launcher._write_status(  # noqa: SLF001 - synthetic persisted crash state
        UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.4",
            phase="starting",
            message="synthetic",
            request_id="pb-upgrade-crashed",
            current_version="0.1.4",
            target_version="0.1.5",
            target_image=_TARGET,
            updated_at="2026-09-16T01:00:00Z",
        )
    )

    status = launcher.status()

    assert status.phase == "manual_recovery_required"
    assert status.rollback_performed is True
