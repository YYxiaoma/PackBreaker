from backend.app.domain.idempotency import (
    candidate_execution_key,
    file_operation_key,
    task_idempotency_key,
)


def test_task_key_is_stable_and_hex_digest() -> None:
    first = task_idempotency_key(
        task_type="PACKAGE_UNPACK",
        source_downloader_id="downloader-1",
        source_hash="source-hash",
        normalized_unit_key="movie-2026",
    )
    second = task_idempotency_key(
        task_type="PACKAGE_UNPACK",
        source_downloader_id="downloader-1",
        source_hash="source-hash",
        normalized_unit_key="movie-2026",
    )

    assert first == second
    assert len(first) == 64
    assert int(first, 16) >= 0


def test_length_prefix_prevents_part_boundary_ambiguity() -> None:
    first = task_idempotency_key(
        task_type="A",
        source_downloader_id="BC",
        source_hash="D",
        normalized_unit_key="E",
    )
    second = task_idempotency_key(
        task_type="AB",
        source_downloader_id="C",
        source_hash="D",
        normalized_unit_key="E",
    )

    assert first != second


def test_execution_and_file_keys_are_scoped() -> None:
    task_key = task_idempotency_key(
        task_type="PACKAGE_UNPACK",
        source_downloader_id="source",
        source_hash="hash",
        normalized_unit_key="unit",
    )
    candidate_key = candidate_execution_key(
        task_key=task_key,
        site_id="site",
        remote_torrent_id="torrent",
        target_downloader_id="target",
    )

    link_key = file_operation_key(
        candidate_key=candidate_key,
        operation_type="HARDLINK",
        normalized_target_path="movies/title.mkv",
    )
    other_key = file_operation_key(
        candidate_key=candidate_key,
        operation_type="CREATE_DIRECTORY",
        normalized_target_path="movies/title.mkv",
    )

    assert candidate_key != task_key
    assert link_key != other_key
