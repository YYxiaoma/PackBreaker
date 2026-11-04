from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import suppress
from pathlib import Path

_MANUAL_KEY_RE = re.compile(r"[0-9a-f]{64}")
MAX_MANUAL_TORRENT_BYTES = 32 * 1024 * 1024


class ManualTorrentStoreError(RuntimeError):
    """A manually supplied torrent could not be safely stored or loaded."""


class ManualTorrentStore:
    """Private, content-addressed storage for user-confirmed .torrent payloads."""

    def __init__(self, data_root: Path, *, max_bytes: int = MAX_MANUAL_TORRENT_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._root = Path(data_root) / "unpack-manual-torrents"
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @staticmethod
    def key_for(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def is_valid_key(key: object) -> bool:
        return isinstance(key, str) and _MANUAL_KEY_RE.fullmatch(key) is not None

    def store(self, payload: bytes) -> str:
        if not payload or len(payload) > self._max_bytes:
            raise ManualTorrentStoreError("manual torrent payload size is invalid")
        key = self.key_for(payload)
        root = self._ensure_root(create=True)
        assert root is not None
        destination = root / f"{key}.torrent"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(destination, flags, 0o600)
        except FileExistsError:
            existing = self.load(key)
            if existing != payload:
                raise ManualTorrentStoreError("manual torrent content-address collision") from None
            return key
        except OSError as exc:
            raise ManualTorrentStoreError("manual torrent could not be stored safely") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            with suppress(OSError):
                destination.unlink(missing_ok=True)
            raise ManualTorrentStoreError("manual torrent could not be stored safely") from exc
        return key

    def load(self, key: str) -> bytes:
        if not self.is_valid_key(key):
            raise ManualTorrentStoreError("manual torrent reference is invalid")
        root = self._ensure_root(create=False)
        assert root is not None
        destination = root / f"{key}.torrent"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(destination, flags)
        except OSError as exc:
            raise ManualTorrentStoreError("manual torrent payload is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size < 1
                or metadata.st_size > self._max_bytes
            ):
                os.close(descriptor)
                raise ManualTorrentStoreError(
                    "manual torrent payload is not a bounded regular file"
                )
            with os.fdopen(descriptor, "rb") as stream:
                payload = stream.read(self._max_bytes + 1)
        except OSError as exc:
            with suppress(OSError):
                os.close(descriptor)
            raise ManualTorrentStoreError("manual torrent payload is unavailable") from exc
        if len(payload) > self._max_bytes or self.key_for(payload) != key:
            raise ManualTorrentStoreError("manual torrent payload integrity check failed")
        return payload

    def delete(self, key: str) -> None:
        if not self.is_valid_key(key):
            return
        root = self._ensure_root(create=False, missing_ok=True)
        if root is None:
            return
        destination = root / f"{key}.torrent"
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ManualTorrentStoreError(
                "manual torrent payload could not be removed safely"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ManualTorrentStoreError("manual torrent payload is not a regular file")
        try:
            destination.unlink()
        except OSError as exc:
            raise ManualTorrentStoreError(
                "manual torrent payload could not be removed safely"
            ) from exc

    def _ensure_root(self, *, create: bool, missing_ok: bool = False) -> Path | None:
        try:
            if create:
                self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self._root.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ManualTorrentStoreError("manual torrent storage is unavailable") from None
        except OSError as exc:
            raise ManualTorrentStoreError("manual torrent storage is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ManualTorrentStoreError("manual torrent storage is not a safe directory")
        return self._root
