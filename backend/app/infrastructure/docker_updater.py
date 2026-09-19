from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

import httpx2

from backend.app.infrastructure.backups import BackupArtifact, create_consistent_backup
from backend.app.infrastructure.updater_protocol import UpgradeHelperRequest

_IMAGE_REPOSITORY_PATTERN = (
    r"(?:(?:localhost|[a-z0-9.-]+)(?::[0-9]{1,5})?/)?[a-z0-9._-]+(?:/[a-z0-9._-]+)*"
)
_DOCKER_DIGEST_IMAGE = re.compile(rf"^(?P<repo>{_IMAGE_REPOSITORY_PATTERN})@sha256:[0-9a-f]{{64}}$")
_IMAGE_REPOSITORY = re.compile(rf"^{_IMAGE_REPOSITORY_PATTERN}$")
_HOST_CONFIG_KEYS = (
    "Binds",
    "Mounts",
    "PortBindings",
    "RestartPolicy",
    "NetworkMode",
    "SecurityOpt",
    "CapAdd",
    "CapDrop",
    "GroupAdd",
    "ReadonlyRootfs",
    "Tmpfs",
    "ShmSize",
    "Init",
    "Dns",
    "DnsOptions",
    "DnsSearch",
    "ExtraHosts",
    "LogConfig",
    "Privileged",
    "Runtime",
    "OomKillDisable",
    "OomScoreAdj",
    "Memory",
    "MemorySwap",
    "MemoryReservation",
    "NanoCpus",
    "CpuShares",
    "CpuPeriod",
    "CpuQuota",
    "CpusetCpus",
    "CpusetMems",
    "PidsLimit",
    "Ulimits",
)
_CONFIG_OVERRIDE_KEYS = (
    "User",
    "Cmd",
    "Entrypoint",
    "WorkingDir",
    "Domainname",
    "StopSignal",
    "StopTimeout",
    "Tty",
    "OpenStdin",
    "StdinOnce",
)


class DockerUpdaterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReplacementPlan:
    container_id: str
    container_name: str
    old_image_id: str
    old_image_reference: str
    create_payload: dict[str, Any]
    config_bind: str


@dataclass(frozen=True, slots=True)
class UpgradeOutcome:
    phase: Literal["succeeded", "failed", "rolled_back", "manual_recovery_required"]
    message: str
    rollback_performed: bool
    quiesced_backup_file: str | None = None


class DockerEngineClient:
    def __init__(
        self,
        docker_socket: Path = Path("/var/run/docker.sock"),
        *,
        timeout_seconds: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Docker API 超时必须大于 0")
        self._docker_socket = docker_socket
        self._transport = transport or httpx2.HTTPTransport(uds=str(docker_socket))
        self._client = httpx2.Client(
            transport=self._transport,
            base_url="http://docker",
            timeout=timeout_seconds,
            headers={"User-Agent": "PackBreaker-Updater"},
        )
        self._api_prefix: str | None = None

    def __enter__(self) -> DockerEngineClient:
        self.negotiate()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def negotiate(self) -> str:
        response = self._client.get("/version")
        self._require(response, {200}, "DOCKER_VERSION_FAILED", "无法读取 Docker Engine 版本")
        payload = _json_object(response)
        api_version = payload.get("ApiVersion")
        if not isinstance(api_version, str) or re.fullmatch(r"\d+\.\d+", api_version) is None:
            raise DockerUpdaterError("DOCKER_API_VERSION_INVALID", "Docker Engine ApiVersion 无效")
        self._api_prefix = f"/v{api_version}"
        return api_version

    def inspect_container(self, container: str) -> dict[str, Any]:
        response = self._client.get(self._path(f"/containers/{quote(container, safe='')}/json"))
        self._require(response, {200}, "DOCKER_CONTAINER_INSPECT_FAILED", "无法读取目标容器配置")
        return _json_object(response)

    def inspect_image(self, image: str) -> dict[str, Any]:
        response = self._client.get(self._path(f"/images/{quote(image, safe='')}/json"))
        self._require(response, {200}, "DOCKER_IMAGE_INSPECT_FAILED", "无法读取当前镜像配置")
        return _json_object(response)

    def pull_image(self, image: str, *, timeout_seconds: float = 900.0) -> None:
        response = self._client.post(
            self._path("/images/create"),
            params={"fromImage": image},
            timeout=timeout_seconds,
        )
        self._require(response, {200}, "DOCKER_IMAGE_PULL_FAILED", "目标镜像拉取失败")
        for line in response.text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and (item.get("error") or item.get("errorDetail")):
                raise DockerUpdaterError(
                    "DOCKER_IMAGE_PULL_FAILED", "Docker daemon 报告目标镜像拉取失败"
                )

    def stop_container(self, container: str, *, timeout_seconds: int = 30) -> None:
        response = self._client.post(
            self._path(f"/containers/{quote(container, safe='')}/stop"),
            params={"t": timeout_seconds},
        )
        self._require(
            response, {204, 304}, "DOCKER_CONTAINER_STOP_FAILED", "停止 PackBreaker 容器失败"
        )

    def rename_container(self, container: str, new_name: str) -> None:
        response = self._client.post(
            self._path(f"/containers/{quote(container, safe='')}/rename"),
            params={"name": new_name},
        )
        self._require(
            response, {204}, "DOCKER_CONTAINER_RENAME_FAILED", "重命名 PackBreaker 容器失败"
        )

    def create_container(self, name: str, payload: Mapping[str, Any]) -> str:
        response = self._client.post(
            self._path("/containers/create"),
            params={"name": name},
            json=dict(payload),
        )
        self._require(
            response, {201}, "DOCKER_CONTAINER_CREATE_FAILED", "创建新 PackBreaker 容器失败"
        )
        result = _json_object(response)
        container_id = result.get("Id")
        if not isinstance(container_id, str) or not container_id:
            raise DockerUpdaterError("DOCKER_CONTAINER_CREATE_FAILED", "Docker 未返回新容器 ID")
        return container_id

    def start_container(self, container: str) -> None:
        response = self._client.post(self._path(f"/containers/{quote(container, safe='')}/start"))
        self._require(
            response, {204, 304}, "DOCKER_CONTAINER_START_FAILED", "启动 PackBreaker 容器失败"
        )

    def remove_container(self, container: str, *, force: bool = False) -> None:
        response = self._client.delete(
            self._path(f"/containers/{quote(container, safe='')}"),
            params={"force": str(force).lower(), "v": "false"},
        )
        self._require(response, {204, 404}, "DOCKER_CONTAINER_REMOVE_FAILED", "删除容器失败")

    def wait_container_exit(self, container: str, *, timeout_seconds: float = 120.0) -> int:
        response = self._client.post(
            self._path(f"/containers/{quote(container, safe='')}/wait"),
            params={"condition": "not-running"},
            timeout=timeout_seconds,
        )
        self._require(response, {200}, "DOCKER_CONTAINER_WAIT_FAILED", "等待维护容器结束失败")
        payload = _json_object(response)
        status = payload.get("StatusCode")
        if not isinstance(status, int):
            raise DockerUpdaterError("DOCKER_CONTAINER_WAIT_FAILED", "维护容器退出码无效")
        return status

    def wait_healthy(
        self,
        container: str,
        *,
        timeout_seconds: float = 180.0,
        poll_seconds: float = 2.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_status = "unknown"
        while time.monotonic() < deadline:
            inspected = self.inspect_container(container)
            state = inspected.get("State")
            if not isinstance(state, dict):
                raise DockerUpdaterError("DOCKER_HEALTH_INVALID", "Docker 容器 State 无效")
            if state.get("Running") is not True:
                raise DockerUpdaterError("DOCKER_NEW_CONTAINER_EXITED", "新 PackBreaker 容器已退出")
            health = state.get("Health")
            if not isinstance(health, dict):
                raise DockerUpdaterError("DOCKER_HEALTHCHECK_MISSING", "新镜像没有可用 healthcheck")
            status = health.get("Status")
            last_status = status if isinstance(status, str) else "unknown"
            if status == "healthy":
                return
            if status == "unhealthy":
                raise DockerUpdaterError(
                    "DOCKER_NEW_CONTAINER_UNHEALTHY", "新 PackBreaker 容器健康检查失败"
                )
            time.sleep(poll_seconds)
        raise DockerUpdaterError(
            "DOCKER_HEALTH_TIMEOUT",
            f"等待新 PackBreaker 容器健康检查超时（last={last_status}）",
        )

    def run_restore_container(
        self,
        *,
        image_id: str,
        config_bind: str,
        database_file: str,
        manifest_file: str,
        name: str,
    ) -> None:
        payload = {
            "Image": image_id,
            "User": "0:0",
            "Entrypoint": ["python", "-m", "backend.app.maintenance"],
            "Cmd": [
                "restore-backup",
                f"/config/backups/pre-upgrade-helper/{database_file}",
                "--manifest",
                f"/config/backups/pre-upgrade-helper/{manifest_file}",
                "--confirm-replace-current-database",
            ],
            "Env": ["PACKBREAKER_CONFIG_DIR=/config", "PACKBREAKER_DATA_DIR=/data"],
            "HostConfig": {
                "Binds": [config_bind],
                "NetworkMode": "none",
                "AutoRemove": False,
                "ReadonlyRootfs": False,
            },
        }
        restore_id = self.create_container(name, payload)
        try:
            self.start_container(restore_id)
            status = self.wait_container_exit(restore_id)
            if status != 0:
                raise DockerUpdaterError(
                    "DOCKER_ROLLBACK_DATABASE_RESTORE_FAILED",
                    f"旧镜像数据库恢复容器退出码为 {status}",
                )
        finally:
            self.remove_container(restore_id, force=True)

    def _path(self, path: str) -> str:
        if self._api_prefix is None:
            self.negotiate()
        return f"{self._api_prefix}{path}"

    @staticmethod
    def _require(
        response: httpx2.Response,
        expected: set[int],
        code: str,
        message: str,
    ) -> None:
        if response.status_code in expected:
            return
        # Docker daemon 的原始错误可能包含宿主机路径、registry 细节或其他部署信息；
        # helper 状态会展示到管理界面，因此这里只返回稳定的脱敏错误码与文案。
        raise DockerUpdaterError(code, message)


def image_repository_is_valid(value: str) -> bool:
    """验证不带 tag/digest 的 Docker repository；允许私有 registry 的显式端口。"""

    normalized = value.lower()
    if _IMAGE_REPOSITORY.fullmatch(normalized) is None:
        return False
    first_component = normalized.split("/", 1)[0]
    if ":" not in first_component:
        return True
    _host, separator, port_text = first_component.rpartition(":")
    if not separator or not port_text.isdigit():
        return False
    port = int(port_text)
    return 1 <= port <= 65535


def digest_image_repository(value: str) -> str | None:
    """返回合法不可变镜像引用中的 repository，否则返回 None。"""

    match = _DOCKER_DIGEST_IMAGE.fullmatch(value.lower())
    if match is None:
        return None
    repository = match.group("repo")
    return repository if image_repository_is_valid(repository) else None


def build_replacement_plan(
    container: Mapping[str, Any],
    old_image: Mapping[str, Any],
    *,
    target_image: str,
    allowed_image: str,
    preserve_docker_socket: bool = False,
) -> ReplacementPlan:
    target_repository = digest_image_repository(target_image)
    if target_repository is None or target_repository != allowed_image.lower():
        raise DockerUpdaterError(
            "UPGRADE_TARGET_IMAGE_UNTRUSTED", "目标镜像必须是官方不可变 digest"
        )

    container_id = _required_string(container, "Id", "目标容器 ID 无效")
    raw_name = _required_string(container, "Name", "目标容器名称无效")
    container_name = raw_name.removeprefix("/")
    if not container_name:
        raise DockerUpdaterError("UPGRADE_CONTAINER_INVALID", "目标容器名称为空")
    old_image_id = _required_string(container, "Image", "当前镜像 ID 无效")
    config = _required_mapping(container, "Config", "目标容器 Config 无效")
    host = _required_mapping(container, "HostConfig", "目标容器 HostConfig 无效")
    old_image_config = _optional_mapping(old_image.get("Config"))
    if config.get("MacAddress") not in (None, ""):
        raise DockerUpdaterError(
            "UPGRADE_STATIC_MAC_ADDRESS_UNSUPPORTED",
            "显式静态 MAC 地址无法在保留旧容器用于回滚时安全复用",
        )
    old_image_reference = _required_string(config, "Image", "当前镜像引用无效")
    normalized_old = old_image_reference.lower()
    if not (
        normalized_old == allowed_image.lower()
        or normalized_old.startswith(f"{allowed_image.lower()}:")
        or normalized_old.startswith(f"{allowed_image.lower()}@")
    ):
        raise DockerUpdaterError(
            "UPGRADE_CURRENT_IMAGE_UNTRUSTED", "当前容器不是官方 PackBreaker 镜像"
        )
    if bool(host.get("AutoRemove")):
        raise DockerUpdaterError(
            "UPGRADE_AUTOREMOVE_UNSUPPORTED", "AutoRemove 容器无法提供可靠回滚"
        )
    network_mode = host.get("NetworkMode")
    if isinstance(network_mode, str) and network_mode.startswith("container:"):
        raise DockerUpdaterError(
            "UPGRADE_NETWORK_MODE_UNSUPPORTED", "container: 网络模式无法安全自动重建"
        )
    for namespace_key in ("PidMode", "IpcMode"):
        value = host.get(namespace_key)
        if isinstance(value, str) and value.startswith("container:"):
            raise DockerUpdaterError(
                "UPGRADE_NAMESPACE_MODE_UNSUPPORTED",
                f"{namespace_key}=container: 无法安全自动重建",
            )

    networks = _optional_mapping(
        _optional_mapping(container.get("NetworkSettings")).get("Networks")
    )
    if len(networks) > 1:
        raise DockerUpdaterError(
            "UPGRADE_MULTI_NETWORK_UNSUPPORTED", "当前版本暂不自动重建多网络容器"
        )

    networking_config = _networking_config(networks)

    config_bind = _config_bind(container)
    create_payload: dict[str, Any] = {"Image": target_image}
    if networking_config is not None:
        create_payload["NetworkingConfig"] = networking_config
    for key in _CONFIG_OVERRIDE_KEYS:
        current = config.get(key)
        default = old_image_config.get(key)
        if current != default and current not in (None, "", [], {}):
            create_payload[key] = current

    env_overrides = _environment_overrides(config.get("Env"), old_image_config.get("Env"))
    if env_overrides:
        create_payload["Env"] = env_overrides
    label_overrides = _mapping_overrides(config.get("Labels"), old_image_config.get("Labels"))
    if label_overrides:
        create_payload["Labels"] = label_overrides

    port_bindings = host.get("PortBindings")
    exposed_ports: set[str] = set()
    if isinstance(port_bindings, dict):
        exposed_ports.update(str(key) for key in port_bindings)
    current_exposed = _optional_mapping(config.get("ExposedPorts"))
    default_exposed = _optional_mapping(old_image_config.get("ExposedPorts"))
    exposed_ports.update(key for key in current_exposed if key not in default_exposed)
    if exposed_ports:
        create_payload["ExposedPorts"] = {key: {} for key in sorted(exposed_ports)}

    host_payload: dict[str, Any] = {"AutoRemove": False}
    for key in _HOST_CONFIG_KEYS:
        if key not in host:
            continue
        value = host.get(key)
        if key == "Binds":
            value = _sanitize_binds(value, preserve_docker_socket=preserve_docker_socket)
        elif key == "Mounts":
            value = _sanitize_mounts(value, preserve_docker_socket=preserve_docker_socket)
        if value not in (None, [], {}):
            host_payload[key] = value
    create_payload["HostConfig"] = host_payload
    return ReplacementPlan(
        container_id=container_id,
        container_name=container_name,
        old_image_id=old_image_id,
        old_image_reference=old_image_reference,
        create_payload=create_payload,
        config_bind=config_bind,
    )


class DockerUpgradeBackend(Protocol):
    def inspect_container(self, container: str) -> dict[str, Any]: ...

    def inspect_image(self, image: str) -> dict[str, Any]: ...

    def pull_image(self, image: str, *, timeout_seconds: float = 900.0) -> None: ...

    def stop_container(self, container: str, *, timeout_seconds: int = 30) -> None: ...

    def rename_container(self, container: str, new_name: str) -> None: ...

    def create_container(self, name: str, payload: Mapping[str, Any]) -> str: ...

    def start_container(self, container: str) -> None: ...

    def remove_container(self, container: str, *, force: bool = False) -> None: ...

    def wait_healthy(
        self,
        container: str,
        *,
        timeout_seconds: float = 180.0,
        poll_seconds: float = 2.0,
    ) -> None: ...

    def run_restore_container(
        self,
        *,
        image_id: str,
        config_bind: str,
        database_file: str,
        manifest_file: str,
        name: str,
    ) -> None: ...


class BackupFactory(Protocol):
    def __call__(
        self,
        database_path: Path,
        backup_dir: Path,
        *,
        app_version: str,
    ) -> BackupArtifact: ...


class DockerUpgradeExecutor:
    def __init__(
        self,
        docker: DockerUpgradeBackend,
        *,
        target_container: str,
        allowed_image: str,
        config_dir: Path,
        backup_factory: BackupFactory = create_consistent_backup,
        health_timeout_seconds: float = 180.0,
        preserve_docker_socket: bool = False,
    ) -> None:
        self._docker = docker
        self._target_container = target_container
        self._allowed_image = allowed_image.lower()
        self._config_dir = config_dir
        self._backup_factory = backup_factory
        self._health_timeout_seconds = health_timeout_seconds
        self._preserve_docker_socket = preserve_docker_socket

    def execute(
        self,
        request: UpgradeHelperRequest,
        *,
        phase: Callable[[str, str], None],
    ) -> UpgradeOutcome:
        inspected = self._docker.inspect_container(self._target_container)
        old_image = self._docker.inspect_image(
            _required_string(inspected, "Image", "当前镜像 ID 无效")
        )
        plan = build_replacement_plan(
            inspected,
            old_image,
            target_image=request.target_image,
            allowed_image=self._allowed_image,
            preserve_docker_socket=self._preserve_docker_socket,
        )
        if plan.container_name != self._target_container:
            raise DockerUpdaterError(
                "UPGRADE_CONTAINER_IDENTITY_MISMATCH", "helper 目标容器名称与实际容器不一致"
            )

        phase("pulling", "正在拉取并验证目标不可变镜像")
        self._docker.pull_image(request.target_image)
        target_image_info = self._docker.inspect_image(request.target_image)
        old_arch = old_image.get("Architecture")
        target_arch = target_image_info.get("Architecture")
        if (
            old_image.get("Os") != "linux"
            or target_image_info.get("Os") != "linux"
            or old_arch not in {"amd64", "arm64"}
            or target_arch != old_arch
        ):
            raise DockerUpdaterError(
                "UPGRADE_PLATFORM_MISMATCH",
                "目标镜像与原运行镜像的 Linux CPU 架构不一致，禁止停止旧容器",
            )
        old_backup_name = f"{plan.container_name}-rollback-{request.request_id[:12]}"
        new_container_id: str | None = None
        old_renamed = False
        old_stopped = False
        database_may_have_changed = False
        quiesced_backup: BackupArtifact | None = None
        try:
            phase("stopping", "正在停止旧容器并创建切换瞬间数据库备份")
            self._docker.stop_container(plan.container_id)
            old_stopped = True
            quiesced_backup = self._backup_factory(
                self._config_dir / "packbreaker.db",
                self._config_dir / "backups" / "pre-upgrade-helper",
                app_version=request.current_version,
            )
            self._docker.rename_container(plan.container_id, old_backup_name)
            old_renamed = True

            phase("starting", "正在按原部署参数创建并启动新容器")
            new_container_id = self._docker.create_container(
                plan.container_name, plan.create_payload
            )
            self._docker.start_container(new_container_id)
            database_may_have_changed = True

            phase("verifying", "正在等待新容器 readiness / healthcheck")
            self._docker.wait_healthy(
                new_container_id,
                timeout_seconds=self._health_timeout_seconds,
            )
            cleanup_warning = False
            try:
                self._docker.remove_container(plan.container_id, force=False)
            except DockerUpdaterError:
                # 新容器已经通过健康检查，此时旧容器只以 rollback 名称保持停止状态。
                # 清理失败不应把一次已经成功的升级反向回滚；后续可人工删除该停止容器。
                cleanup_warning = True
            return UpgradeOutcome(
                phase="succeeded",
                message=(
                    (
                        f"PackBreaker 已升级到 {request.target_version}；"
                        "旧停止容器清理失败，可稍后人工删除"
                    )
                    if cleanup_warning
                    else f"PackBreaker 已升级到 {request.target_version}"
                ),
                rollback_performed=False,
                quiesced_backup_file=(
                    None if quiesced_backup is None else quiesced_backup.database_path.name
                ),
            )
        except Exception as upgrade_exc:
            if not old_stopped:
                return UpgradeOutcome(
                    phase="failed",
                    message=f"升级在停止旧容器前失败：{_safe_error_text(upgrade_exc)}",
                    rollback_performed=False,
                )
            phase("rolling_back", "新版本未通过健康检查，正在恢复旧版本与升级前数据库")
            try:
                if new_container_id is not None:
                    with suppress(DockerUpdaterError):
                        self._docker.stop_container(new_container_id, timeout_seconds=10)
                    self._docker.remove_container(new_container_id, force=True)
                if database_may_have_changed:
                    if quiesced_backup is None:
                        raise DockerUpdaterError(
                            "UPGRADE_ROLLBACK_BACKUP_MISSING",
                            "缺少切换瞬间数据库备份",
                        )
                    self._docker.run_restore_container(
                        image_id=plan.old_image_id,
                        config_bind=plan.config_bind,
                        database_file=quiesced_backup.database_path.name,
                        manifest_file=quiesced_backup.manifest_path.name,
                        name=f"{plan.container_name}-db-restore-{request.request_id[:10]}",
                    )
                if old_renamed:
                    self._docker.rename_container(plan.container_id, plan.container_name)
                self._docker.start_container(plan.container_id)
                self._docker.wait_healthy(
                    plan.container_id,
                    timeout_seconds=self._health_timeout_seconds,
                )
            except Exception as rollback_exc:
                return UpgradeOutcome(
                    phase="manual_recovery_required",
                    message=(
                        "自动升级失败且自动回滚未完成；请保留现场并按备份恢复流程处理。"
                        f" upgrade={_safe_error_text(upgrade_exc)}"
                        f" rollback={_safe_error_text(rollback_exc)}"
                    ),
                    rollback_performed=True,
                    quiesced_backup_file=(
                        None if quiesced_backup is None else quiesced_backup.database_path.name
                    ),
                )
            return UpgradeOutcome(
                phase="rolled_back",
                message=f"新版本启动失败，已自动恢复旧版本：{_safe_error_text(upgrade_exc)}",
                rollback_performed=True,
                quiesced_backup_file=(
                    None if quiesced_backup is None else quiesced_backup.database_path.name
                ),
            )


def _networking_config(networks: Mapping[str, Any]) -> dict[str, Any] | None:
    if not networks:
        return None
    network_name, endpoint = next(iter(networks.items()))
    if not isinstance(network_name, str) or not network_name or not isinstance(endpoint, dict):
        raise DockerUpdaterError("UPGRADE_NETWORK_INVALID", "Docker 网络配置无效")
    if endpoint.get("IPAMConfig") not in (None, {}):
        raise DockerUpdaterError(
            "UPGRADE_STATIC_NETWORK_ADDRESS_UNSUPPORTED",
            "显式静态网络地址无法在保留旧容器用于回滚时安全复用",
        )
    # NetworkSettings 中的 MacAddress/IPAddress 是 daemon 分配的运行时值，
    # 不能在旧容器仍保留网络 endpoint 时复制给新容器。
    allowed_keys = {"Links", "Aliases", "DriverOpts"}
    sanitized = {
        key: endpoint[key]
        for key in allowed_keys
        if key in endpoint and endpoint[key] not in (None, "", [], {})
    }
    return {"EndpointsConfig": {network_name: sanitized}}


def _config_bind(container: Mapping[str, Any]) -> str:
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        mounts = []
    matches = [
        item for item in mounts if isinstance(item, dict) and item.get("Destination") == "/config"
    ]
    if len(matches) != 1:
        raise DockerUpdaterError(
            "UPGRADE_CONFIG_MOUNT_INVALID", "目标容器必须且只能有一个 /config 挂载"
        )
    mount = matches[0]
    if mount.get("RW") is not True:
        raise DockerUpdaterError("UPGRADE_CONFIG_MOUNT_READONLY", "/config 挂载必须可写")
    mount_type = mount.get("Type")
    if mount_type == "bind":
        source = mount.get("Source")
    elif mount_type == "volume":
        source = mount.get("Name") or mount.get("Source")
    else:
        raise DockerUpdaterError(
            "UPGRADE_CONFIG_MOUNT_UNSUPPORTED", "/config 必须使用 bind 或 volume 挂载"
        )
    if not isinstance(source, str) or not source:
        raise DockerUpdaterError("UPGRADE_CONFIG_MOUNT_INVALID", "/config 挂载来源无效")
    return f"{source}:/config"


def _sanitize_binds(value: object, *, preserve_docker_socket: bool = False) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DockerUpdaterError("UPGRADE_BINDS_INVALID", "Docker Binds 配置无效")
    kept = []
    for bind in cast(list[str], value):
        parts = bind.split(":")
        if not preserve_docker_socket and len(parts) >= 2 and parts[1] == "/var/run/docker.sock":
            continue
        kept.append(bind)
    return kept


def _sanitize_mounts(
    value: object, *, preserve_docker_socket: bool = False
) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise DockerUpdaterError("UPGRADE_MOUNTS_INVALID", "Docker Mounts 配置无效")
    kept: list[dict[str, Any]] = []
    allowed_keys = {
        "Type",
        "Source",
        "Target",
        "ReadOnly",
        "Consistency",
        "BindOptions",
        "VolumeOptions",
        "TmpfsOptions",
    }
    for item in value:
        if not isinstance(item, dict):
            raise DockerUpdaterError("UPGRADE_MOUNTS_INVALID", "Docker Mounts 条目无效")
        target = item.get("Target") or item.get("Destination")
        if target == "/var/run/docker.sock" and not preserve_docker_socket:
            continue
        sanitized = {key: item[key] for key in allowed_keys if key in item}
        if "Target" not in sanitized and isinstance(target, str):
            sanitized["Target"] = target
        kept.append(sanitized)
    return kept


def _environment_overrides(current: object, defaults: object) -> list[str]:
    current_values = _env_map(current)
    default_values = _env_map(defaults)
    return [
        f"{key}={value}"
        for key, value in current_values.items()
        if default_values.get(key) != value
    ]


def _env_map(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DockerUpdaterError("UPGRADE_ENV_INVALID", "Docker Env 配置无效")
    result: dict[str, str] = {}
    for item in cast(list[str], value):
        key, separator, item_value = item.partition("=")
        if not separator or not key:
            continue
        result[key] = item_value
    return result


def _mapping_overrides(current: object, defaults: object) -> dict[str, str]:
    current_map = _optional_mapping(current)
    default_map = _optional_mapping(defaults)
    result: dict[str, str] = {}
    for key, value in current_map.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if default_map.get(key) != value:
            result[key] = value
    return result


def _required_mapping(payload: Mapping[str, Any], key: str, message: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise DockerUpdaterError("UPGRADE_CONTAINER_INVALID", message)
    return cast(dict[str, Any], value)


def _optional_mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _required_string(payload: Mapping[str, Any], key: str, message: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DockerUpdaterError("UPGRADE_CONTAINER_INVALID", message)
    return value


def _json_object(response: httpx2.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DockerUpdaterError(
            "DOCKER_RESPONSE_INVALID", "Docker daemon 返回了无效 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise DockerUpdaterError("DOCKER_RESPONSE_INVALID", "Docker daemon 响应必须是 JSON object")
    return cast(dict[str, Any], payload)


def _safe_error_text(exc: BaseException) -> str:
    if isinstance(exc, DockerUpdaterError):
        return f"{exc.code}:{exc}"
    return type(exc).__name__
