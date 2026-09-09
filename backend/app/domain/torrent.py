from dataclasses import dataclass
from enum import StrEnum


class TorrentKind(StrEnum):
    V1 = "V1"
    V2 = "V2"
    HYBRID = "HYBRID"


@dataclass(frozen=True, slots=True)
class TorrentFile:
    path: str
    raw_path_hex: tuple[str, ...]
    length: int
    padding: bool
    zero_length: bool
    order: int
    pieces_root: bytes | None = None


@dataclass(frozen=True, slots=True)
class PieceLayer:
    pieces_root: bytes
    hashes: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class TorrentMeta:
    torrent_kind: TorrentKind
    v1_info_hash: str | None
    v2_info_hash: str | None
    piece_length: int
    files: tuple[TorrentFile, ...]
    v1_piece_hashes: tuple[bytes, ...]
    v2_piece_layers: tuple[PieceLayer, ...]
    private: bool
    source: str | None
    display_name: str
    metainfo_digest: str
    info_span: tuple[int, int]
