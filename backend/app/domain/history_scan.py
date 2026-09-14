from __future__ import annotations

from enum import StrEnum


class HistoryMediaKind(StrEnum):
    MOVIE = "MOVIE"
    EPISODE = "EPISODE"


class HistoryScanStatus(StrEnum):
    READY = "READY"
    SCANNING = "SCANNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
