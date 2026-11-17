from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_manual_torrents import ManualTorrentStore
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecutionItem,
    UnpackMatchCandidate,
)

_VERIFIED_TORRENT_STORAGE_KEY = "verified_torrent_storage_key"
_MANUAL_TORRENT_STORAGE_KEY = "manual_torrent_storage_key"


def candidate_torrent_storage_key(raw_ref: Mapping[str, Any]) -> str | None:
    """Return the private content-addressed torrent key bound to this candidate."""
    manual_key = raw_ref.get(_MANUAL_TORRENT_STORAGE_KEY)
    verified_key = raw_ref.get(_VERIFIED_TORRENT_STORAGE_KEY)
    if manual_key is not None and verified_key is not None and manual_key != verified_key:
        raise ValueError("候选包含相互冲突的 torrent 存储引用")
    key = manual_key if manual_key is not None else verified_key
    if key is not None and not ManualTorrentStore.is_valid_key(key):
        raise ValueError("候选 torrent 存储引用无效")
    return key


def persist_candidate_torrent_storage_key(
    session_factory: sessionmaker[Session],
    *,
    item_id: str,
    candidate_id: str,
    generation: int,
    expected_item_version: int,
    storage_key: str,
) -> None:
    """Bind a fetched torrent to the exact still-current item/candidate generation."""
    if not ManualTorrentStore.is_valid_key(storage_key):
        raise ValueError("torrent 存储摘要无效")
    with session_factory() as session:
        begin_immediate_write(session)
        item = session.get(UnpackExecutionItem, item_id)
        candidate = session.get(UnpackMatchCandidate, candidate_id)
        if (
            item is None
            or candidate is None
            or item.version != expected_item_version
            or item.selected_candidate_id != candidate_id
            or item.candidate_generation != generation
            or candidate.item_id != item_id
            or candidate.generation != generation
        ):
            raise ApplicationError(
                code="UNPACK_TORRENT_STORAGE_CONFLICT",
                status=409,
                title="候选 torrent 关联已变化",
                detail="保存 torrent 复用引用时，影片项或候选代次已经变化",
            )
        raw_ref = dict(candidate.raw_ref or {})
        manual_key = raw_ref.get(_MANUAL_TORRENT_STORAGE_KEY)
        existing_key = raw_ref.get(_VERIFIED_TORRENT_STORAGE_KEY)
        if manual_key is not None:
            if manual_key != storage_key:
                raise ApplicationError(
                    code="UNPACK_TORRENT_STORAGE_CONFLICT",
                    status=409,
                    title="候选 torrent 存储引用冲突",
                    detail="手动上传的 torrent 与待保存的内容摘要不一致",
                )
            return
        if existing_key is not None and existing_key != storage_key:
            raise ApplicationError(
                code="UNPACK_TORRENT_STORAGE_CONFLICT",
                status=409,
                title="候选 torrent 存储引用冲突",
                detail="同一候选已经绑定另一份不可变 torrent 内容",
            )
        if existing_key == storage_key:
            return
        candidate.raw_ref = {**raw_ref, _VERIFIED_TORRENT_STORAGE_KEY: storage_key}
        session.commit()
