from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from backend.app.domain.torrent import TorrentMeta

AUXILIARY_EXTENSIONS = frozenset(
    {
        ".nfo",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".srt",
        ".ass",
        ".ssa",
        ".sub",
        ".idx",
        ".sup",
        ".txt",
    }
)


@dataclass(frozen=True, slots=True)
class AuxiliaryFetchPlan:
    wanted_indices: tuple[int, ...]
    unwanted_indices: tuple[int, ...]
    missing_paths: tuple[str, ...]


def is_auxiliary_torrent_path(path: str) -> bool:
    return PurePosixPath(path).suffix.casefold() in AUXILIARY_EXTENSIONS


def build_auxiliary_fetch_plan(
    meta: TorrentMeta,
    missing_paths: tuple[str, ...],
) -> AuxiliaryFetchPlan:
    normalized_missing = tuple(dict.fromkeys(path for path in missing_paths if path))
    if not normalized_missing:
        raise ValueError("辅助文件补齐计划不能为空")
    file_index = {item.path: index for index, item in enumerate(meta.files)}
    if len(file_index) != len(meta.files):
        raise ValueError("torrent 文件路径存在重复，无法建立选择计划")
    wanted: list[int] = []
    for path in normalized_missing:
        index = file_index.get(path)
        if index is None:
            raise ValueError("辅助文件路径已不在当前 torrent 中")
        torrent_file = meta.files[index]
        if torrent_file.padding or torrent_file.zero_length:
            raise ValueError("协议 padding / zero-length 不能作为辅助下载目标")
        if not is_auxiliary_torrent_path(path):
            raise ValueError("非辅助文件不能进入自动补齐计划")
        wanted.append(index)
    wanted_set = set(wanted)
    return AuxiliaryFetchPlan(
        wanted_indices=tuple(sorted(wanted_set)),
        unwanted_indices=tuple(
            index for index in range(len(meta.files)) if index not in wanted_set
        ),
        missing_paths=normalized_missing,
    )


def torrent_client_hash(meta: TorrentMeta) -> str:
    value = meta.v1_info_hash or meta.v2_info_hash
    if value is None:
        raise ValueError("torrent 缺少可供下载器识别的 info hash")
    return value
