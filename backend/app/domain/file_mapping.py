from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from backend.app.domain.media_matching import media_file_token_signature
from backend.app.domain.torrent import TorrentFile, TorrentMeta
from backend.app.domain.verification import FileMappingState, FileSnapshot


class MappingMethod(StrEnum):
    EXACT_PATH = "EXACT_PATH"
    BASENAME = "BASENAME"
    MEDIA_TOKENS = "MEDIA_TOKENS"
    NONE = "NONE"
    PROTOCOL_PADDING = "PROTOCOL_PADDING"
    ZERO_LENGTH = "ZERO_LENGTH"


@dataclass(frozen=True, slots=True)
class SourceFileCandidate:
    relative_path: str
    source_path: str
    length: int
    snapshot: FileSnapshot


@dataclass(frozen=True, slots=True)
class AutoMappingDecision:
    torrent_path: str
    state: FileMappingState
    method: MappingMethod
    source_path: str | None
    snapshot: FileSnapshot | None
    candidate_paths: tuple[str, ...] = ()


def auto_map_files(
    meta: TorrentMeta,
    sources: tuple[SourceFileCandidate, ...],
) -> tuple[AutoMappingDecision, ...]:
    """按确定性顺序执行安全自动映射；任何同级并列都返回 AMBIGUOUS。"""

    decisions: list[AutoMappingDecision] = []
    for torrent_file in meta.files:
        decisions.append(_map_one(meta, torrent_file, sources))
    return tuple(decisions)


def _map_one(
    meta: TorrentMeta,
    torrent_file: TorrentFile,
    sources: tuple[SourceFileCandidate, ...],
) -> AutoMappingDecision:
    if torrent_file.padding:
        return AutoMappingDecision(
            torrent_file.path,
            FileMappingState.PADDING,
            MappingMethod.PROTOCOL_PADDING,
            None,
            None,
        )
    if torrent_file.zero_length:
        return AutoMappingDecision(
            torrent_file.path,
            FileMappingState.ZERO_LENGTH,
            MappingMethod.ZERO_LENGTH,
            None,
            None,
        )

    same_length = tuple(item for item in sources if item.length == torrent_file.length)
    exact_names = _exact_relative_variants(meta, torrent_file.path)
    exact = tuple(item for item in same_length if item.relative_path in exact_names)
    exact_decision = _unique_or_ambiguous(torrent_file.path, exact, MappingMethod.EXACT_PATH)
    if exact_decision is not None:
        return exact_decision

    basename = PurePosixPath(torrent_file.path).name
    basename_matches = tuple(
        item for item in same_length if PurePosixPath(item.relative_path).name == basename
    )
    basename_decision = _unique_or_ambiguous(
        torrent_file.path,
        basename_matches,
        MappingMethod.BASENAME,
    )
    if basename_decision is not None:
        return basename_decision

    torrent_basename = PurePosixPath(torrent_file.path).name
    torrent_extension = PurePosixPath(torrent_basename).suffix.casefold()
    torrent_tokens = media_file_token_signature(torrent_basename)
    token_matches = tuple(
        item
        for item in same_length
        if PurePosixPath(item.relative_path).suffix.casefold() == torrent_extension
        and media_file_token_signature(PurePosixPath(item.relative_path).name) == torrent_tokens
    )
    token_decision = _unique_or_ambiguous(
        torrent_file.path,
        token_matches,
        MappingMethod.MEDIA_TOKENS,
    )
    if token_decision is not None:
        return token_decision

    return AutoMappingDecision(
        torrent_file.path,
        FileMappingState.MISSING,
        MappingMethod.NONE,
        None,
        None,
    )


def _exact_relative_variants(meta: TorrentMeta, torrent_path: str) -> frozenset[str]:
    variants = {torrent_path}
    root_prefix = f"{meta.display_name}/"
    if torrent_path.startswith(root_prefix):
        variants.add(torrent_path[len(root_prefix) :])
    return frozenset(variants)


def _unique_or_ambiguous(
    torrent_path: str,
    matches: tuple[SourceFileCandidate, ...],
    method: MappingMethod,
) -> AutoMappingDecision | None:
    if not matches:
        return None
    ordered = tuple(sorted(matches, key=lambda item: (item.relative_path, item.source_path)))
    if len(ordered) > 1:
        return AutoMappingDecision(
            torrent_path,
            FileMappingState.AMBIGUOUS,
            method,
            None,
            None,
            tuple(item.source_path for item in ordered),
        )
    source = ordered[0]
    return AutoMappingDecision(
        torrent_path,
        FileMappingState.MAPPED,
        method,
        source.source_path,
        source.snapshot,
        (source.source_path,),
    )
