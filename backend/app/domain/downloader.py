from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.verification import DownloaderKind


class ProbeStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DownloaderCredential:
    username: str | None = None
    password: str | None = None
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class DownloaderCapabilities:
    client: str
    version: str
    api_version: str | None
    supports_skip_checking: bool
    read_only_probe: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "version": self.version,
            "api_version": self.api_version,
            "supports_skip_checking": self.supports_skip_checking,
            "read_only_probe": self.read_only_probe,
        }


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    capabilities: DownloaderCapabilities


@dataclass(frozen=True, slots=True)
class PathMappingRule:
    remote_prefix: str
    container_prefix: str


@dataclass(frozen=True, slots=True)
class MappingMatch:
    rule_index: int
    container_path: Path
    normalized_remote_path: str


def validate_credential(kind: DownloaderKind, credential: DownloaderCredential | None) -> None:
    if credential is None:
        return
    if kind is DownloaderKind.QBITTORRENT:
        if credential.api_key:
            if credential.username or credential.password:
                raise ValueError("qBittorrent API Key 不能与用户名/密码同时配置")
            return
        if bool(credential.username) != bool(credential.password):
            raise ValueError("qBittorrent 用户名和密码必须同时提供")
        if not credential.username:
            raise ValueError("qBittorrent 凭证不能为空")
        return
    if credential.api_key:
        raise ValueError("Transmission 不支持 qBittorrent API Key 凭证格式")
    if bool(credential.username) != bool(credential.password):
        raise ValueError("Transmission 用户名和密码必须同时提供")
    if not credential.username:
        raise ValueError("Transmission 凭证不能为空")


def normalize_base_url(kind: DownloaderKind, value: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("下载器管理地址必须是 http/https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("下载器管理地址不得包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("下载器管理地址不得包含 query 或 fragment")
    path = parsed.path.rstrip("/")
    if kind is DownloaderKind.TRANSMISSION and not path:
        path = "/transmission/rpc"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def normalize_path_mappings(
    rules: list[PathMappingRule],
    *,
    allowed_root: Path,
) -> list[PathMappingRule]:
    root = allowed_root.resolve(strict=False)
    normalized: list[PathMappingRule] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        remote = _normalize_remote(rule.remote_prefix)
        container = Path(rule.container_prefix)
        if not container.is_absolute():
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "容器路径必须是绝对路径")
        resolved = container.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "容器路径必须位于数据根目录内")
        key = (_remote_key(remote), str(resolved))
        if key in seen:
            raise DomainViolation(ErrorCode.MAPPING_AMBIGUOUS, "存在重复的路径映射规则")
        seen.add(key)
        normalized.append(PathMappingRule(remote_prefix=remote, container_prefix=str(resolved)))
    return normalized


def map_remote_path(
    remote_path: str,
    rules: list[PathMappingRule],
    *,
    allowed_root: Path,
) -> MappingMatch:
    normalized_remote = _normalize_remote(remote_path)
    candidates: list[tuple[int, int, PathMappingRule, tuple[str, ...]]] = []
    for index, rule in enumerate(rules):
        suffix = _relative_remote(normalized_remote, rule.remote_prefix)
        if suffix is not None:
            candidates.append((_remote_depth(rule.remote_prefix), index, rule, suffix))
    if not candidates:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "下载器路径未命中任何映射规则")
    max_depth = max(candidate[0] for candidate in candidates)
    winners = [candidate for candidate in candidates if candidate[0] == max_depth]
    if len(winners) != 1:
        raise DomainViolation(ErrorCode.MAPPING_AMBIGUOUS, "下载器路径同时命中多条等长映射")
    _, index, rule, suffix = winners[0]
    mapped = Path(rule.container_prefix).joinpath(*suffix).resolve(strict=False)
    root = allowed_root.resolve(strict=False)
    if not mapped.is_relative_to(root):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "映射结果越过数据根目录")
    return MappingMatch(index, mapped, normalized_remote)


def reverse_map_container_path(container_path: Path, rule: PathMappingRule) -> str:
    resolved = container_path.resolve(strict=False)
    prefix = Path(rule.container_prefix).resolve(strict=False)
    try:
        suffix = resolved.relative_to(prefix)
    except ValueError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "容器路径无法反向映射") from exc
    if _is_windows_remote(rule.remote_prefix):
        return str(PureWindowsPath(rule.remote_prefix).joinpath(*suffix.parts))
    return str(PurePosixPath(rule.remote_prefix).joinpath(*suffix.parts))


def _normalize_remote(value: str) -> str:
    value = value.strip()
    path = PureWindowsPath(value) if _is_windows_remote(value) else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "下载器路径必须是安全绝对路径")
    return str(path)


def _is_windows_remote(value: str) -> bool:
    return "\\" in value or (len(value) >= 2 and value[1] == ":")


def _remote_key(value: str) -> str:
    return value.casefold() if _is_windows_remote(value) else value


def _remote_depth(value: str) -> int:
    path = PureWindowsPath(value) if _is_windows_remote(value) else PurePosixPath(value)
    return len(path.parts)


def _relative_remote(path_value: str, prefix_value: str) -> tuple[str, ...] | None:
    windows = _is_windows_remote(path_value) or _is_windows_remote(prefix_value)
    if windows:
        windows_path = PureWindowsPath(path_value)
        windows_prefix = PureWindowsPath(prefix_value)
        path_parts = tuple(part.casefold() for part in windows_path.parts)
        prefix_parts = tuple(part.casefold() for part in windows_prefix.parts)
        if len(path_parts) < len(prefix_parts) or path_parts[: len(prefix_parts)] != prefix_parts:
            return None
        return tuple(windows_path.parts[len(prefix_parts) :])
    posix_path = PurePosixPath(path_value)
    posix_prefix = PurePosixPath(prefix_value)
    if (
        len(posix_path.parts) < len(posix_prefix.parts)
        or posix_path.parts[: len(posix_prefix.parts)] != posix_prefix.parts
    ):
        return None
    return tuple(posix_path.parts[len(posix_prefix.parts) :])
