from hashlib import sha256

_KEY_VERSION = "packbreaker-idempotency-v1"


def _digest(namespace: str, *parts: str) -> str:
    """使用长度前缀编码，避免分隔符歧义并只输出不可逆摘要。"""

    payload = bytearray(_KEY_VERSION.encode("utf-8"))
    for value in (namespace, *parts):
        encoded = value.encode("utf-8")
        payload.extend(len(encoded).to_bytes(8, byteorder="big", signed=False))
        payload.extend(encoded)
    return sha256(payload).hexdigest()


def task_idempotency_key(
    *,
    task_type: str,
    source_downloader_id: str,
    source_hash: str,
    normalized_unit_key: str,
) -> str:
    return _digest(
        "task",
        task_type,
        source_downloader_id,
        source_hash,
        normalized_unit_key,
    )


def candidate_execution_key(
    *,
    task_key: str,
    site_id: str,
    remote_torrent_id: str,
    target_downloader_id: str,
) -> str:
    return _digest(
        "candidate",
        task_key,
        site_id,
        remote_torrent_id,
        target_downloader_id,
    )


def file_operation_key(
    *,
    candidate_key: str,
    operation_type: str,
    normalized_target_path: str,
) -> str:
    return _digest(
        "file-operation",
        candidate_key,
        operation_type,
        normalized_target_path,
    )
