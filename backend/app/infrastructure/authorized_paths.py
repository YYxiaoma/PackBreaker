from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from backend.app.domain.errors import DomainViolation, ErrorCode

_PROTECTED_NAMESPACE_ROOTS = (
    Path("/app"),
    Path("/bin"),
    Path("/dev"),
    Path("/etc"),
    Path("/lib"),
    Path("/lib64"),
    Path("/opt"),
    Path("/proc"),
    Path("/run"),
    Path("/sbin"),
    Path("/sys"),
    Path("/usr"),
    Path("/var"),
)
_PROTECTED_EXACT_ROOTS = (Path("/tmp"),)


@dataclass(frozen=True, slots=True)
class AuthorizedDirectoryEntry:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class AuthorizedPathScope:
    """Container path namespace plus the explicit directory mounts PackBreaker may operate on."""

    legacy_data_root: Path
    config_dir: Path
    authorized_roots: tuple[Path, ...]

    @classmethod
    def from_runtime(
        cls,
        *,
        legacy_data_root: Path,
        config_dir: Path,
        mountinfo_path: Path = Path("/proc/self/mountinfo"),
    ) -> AuthorizedPathScope:
        legacy = legacy_data_root.absolute()
        config = config_dir.absolute()
        discovered = _discover_directory_mounts(
            mountinfo_path=mountinfo_path,
            config_dir=config,
        )
        roots = list(discovered)
        if _is_safe_directory_root(legacy, config_dir=config):
            roots.append(legacy)
        return cls(
            legacy_data_root=legacy,
            config_dir=config,
            authorized_roots=_minimal_roots(roots),
        )

    @classmethod
    def legacy_only(
        cls,
        *,
        legacy_data_root: Path,
        config_dir: Path | None = None,
    ) -> AuthorizedPathScope:
        legacy = legacy_data_root.absolute()
        config = (config_dir or legacy.parent / ".packbreaker-config").absolute()
        return cls(
            legacy_data_root=legacy,
            config_dir=config,
            authorized_roots=(legacy,),
        )

    def normalize_reference(self, value: str, *, allow_namespace_root: bool = False) -> str:
        normalized = unicodedata.normalize("NFC", value.strip())
        has_windows_drive = (
            len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
        )
        if not normalized or "\x00" in normalized or "\\" in normalized or has_windows_drive:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "路径必须是安全 POSIX 路径")

        parsed = PurePosixPath(normalized)
        if ".." in parsed.parts:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "路径不能包含 ..")
        if parsed.is_absolute():
            absolute = Path(parsed.as_posix())
        else:
            if normalized == ".":
                absolute = self.legacy_data_root
            else:
                if any(part in {"", "."} for part in parsed.parts):
                    raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "路径包含不安全路径段")
                absolute = self.legacy_data_root.joinpath(*parsed.parts)

        absolute = absolute.absolute()
        if absolute == Path("/"):
            if allow_namespace_root:
                return "/"
            raise DomainViolation(
                ErrorCode.PATH_MAPPING_INVALID, "容器根目录 / 不能直接作为任务目录"
            )
        self.assert_authorized(absolute)
        return absolute.as_posix()

    def resolve_existing_directory(self, value: str) -> tuple[str, Path]:
        reference = self.normalize_reference(value)
        path = Path(reference)
        root = self.authorization_root(path)
        _assert_real_directory_chain(root, path, allow_missing=False)
        return reference, path.resolve(strict=True)

    def resolve_output_anchor(self, value: str) -> tuple[str, Path, bool]:
        reference = self.normalize_reference(value)
        path = Path(reference)
        root = self.authorization_root(path)
        anchor, exists = _assert_real_directory_chain(root, path, allow_missing=True)
        return reference, anchor, exists

    def resolve_path(self, value: str, *, allow_namespace_root: bool = False) -> Path:
        reference = self.normalize_reference(value, allow_namespace_root=allow_namespace_root)
        return Path(reference)

    def authorization_root(self, path: Path) -> Path:
        absolute = path.absolute()
        matches = [
            root
            for root in self.authorized_roots
            if absolute == root or absolute.is_relative_to(root)
        ]
        if not matches:
            raise DomainViolation(
                ErrorCode.PATH_MAPPING_INVALID,
                "路径不在 PackBreaker 显式挂载的授权目录内",
            )
        return max(matches, key=lambda item: len(item.parts))

    def assert_authorized(self, path: Path) -> Path:
        return self.authorization_root(path)

    def browse_directories(
        self, value: str = "/"
    ) -> tuple[str, tuple[AuthorizedDirectoryEntry, ...]]:
        normalized = unicodedata.normalize("NFC", value.strip()) or "/"
        if normalized in {".", "/"}:
            return "/", tuple(
                AuthorizedDirectoryEntry(name=root.as_posix(), path=root.as_posix())
                for root in self.authorized_roots
            )

        reference, resolved = self.resolve_existing_directory(normalized)
        entries: list[AuthorizedDirectoryEntry] = []
        try:
            children = tuple(os.scandir(resolved))
        except OSError as exc:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取所选授权目录") from exc
        for child in sorted(children, key=lambda item: item.name.casefold()):
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                continue
            child_path = Path(reference) / child.name
            entries.append(AuthorizedDirectoryEntry(name=child.name, path=child_path.as_posix()))
        return reference, tuple(entries)


def _discover_directory_mounts(*, mountinfo_path: Path, config_dir: Path) -> tuple[Path, ...]:
    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    roots: list[Path] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        raw_mountpoint = _unescape_mountinfo(fields[4])
        candidate = Path(raw_mountpoint)
        if not candidate.is_absolute() or candidate == Path("/"):
            continue
        if not _is_safe_directory_root(candidate, config_dir=config_dir):
            continue
        roots.append(candidate.absolute())
    return _minimal_roots(roots)


def _is_safe_directory_root(path: Path, *, config_dir: Path) -> bool:
    absolute = path.absolute()
    protected = (*_PROTECTED_NAMESPACE_ROOTS, config_dir.absolute())
    if absolute in _PROTECTED_EXACT_ROOTS:
        return False
    if any(absolute == root or absolute.is_relative_to(root) for root in protected):
        return False
    try:
        item_stat = absolute.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(item_stat.st_mode) and _path_chain_has_no_symlink(absolute)


def _minimal_roots(values: list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
    unique = sorted(
        {value.absolute() for value in values},
        key=lambda item: (len(item.parts), item.as_posix()),
    )
    selected: list[Path] = []
    for candidate in unique:
        if any(candidate == root or candidate.is_relative_to(root) for root in selected):
            continue
        selected.append(candidate)
    return tuple(selected)


def _path_chain_has_no_symlink(path: Path) -> bool:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            item_stat = current.stat(follow_symlinks=False)
        except OSError:
            return False
        if stat.S_ISLNK(item_stat.st_mode):
            return False
    return True


def _assert_real_directory_chain(
    root: Path,
    requested: Path,
    *,
    allow_missing: bool,
) -> tuple[Path, bool]:
    try:
        suffix = requested.relative_to(root)
    except ValueError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "路径越过授权目录") from exc

    if not _path_chain_has_no_symlink(root):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "授权挂载目录不能经过符号链接")
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "授权挂载目录不可用") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "授权挂载目录必须是真实目录")

    current = root
    for part in suffix.parts:
        current = current / part
        try:
            item_stat = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return current.parent.resolve(strict=True), False
            raise DomainViolation(
                ErrorCode.PATH_MAPPING_INVALID, "所选目录不存在或不可读取"
            ) from None
        except OSError as exc:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "所选目录无法安全检查") from exc
        if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
            raise DomainViolation(
                ErrorCode.PATH_MAPPING_INVALID,
                "目录路径不能经过符号链接或非目录节点",
            )
    return current.resolve(strict=True), True


def _unescape_mountinfo(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )
