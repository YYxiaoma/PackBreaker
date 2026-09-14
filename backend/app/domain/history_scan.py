from __future__ import annotations

from enum import StrEnum
from hashlib import sha256


class HistoryMediaKind(StrEnum):
    MOVIE = "MOVIE"
    EPISODE = "EPISODE"


class HistoryScanStatus(StrEnum):
    READY = "READY"
    SCANNING = "SCANNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    DONE = "DONE"


class HistoryMaterializationStatus(StrEnum):
    MATERIALIZED = "MATERIALIZED"
    SKIPPED = "SKIPPED"


def history_file_snapshot_digest(*, device: int, inode: int, size: int, mtime_ns: int) -> str:
    payload = f"{device}\0{inode}\0{size}\0{mtime_ns}".encode()
    return sha256(payload).hexdigest()
