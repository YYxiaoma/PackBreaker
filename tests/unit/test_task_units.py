import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.task_units import SourceTaskFile, TaskUnitKind, identify_task_units


def test_file_pack_is_split_into_movie_and_episode_units_deterministically() -> None:
    files = (
        SourceTaskFile("Pack/Show.S01E02.1080p.mkv", 200),
        SourceTaskFile("Pack/readme.nfo", 10),
        SourceTaskFile("Pack/Movie.2024.2160p.mkv", 300),
        SourceTaskFile("Pack/Movie.2024.zh.srt", 20),
    )

    units = identify_task_units(files)

    assert [item.source_relative_path for item in units] == [
        "Pack/Movie.2024.2160p.mkv",
        "Pack/Show.S01E02.1080p.mkv",
    ]
    assert units[0].kind is TaskUnitKind.MOVIE
    assert units[1].kind is TaskUnitKind.EPISODE
    assert units[0].descriptor.year == 2024
    assert units[1].descriptor.episode is not None


def test_unit_key_is_stable_and_changes_with_identity_input() -> None:
    first = identify_task_units((SourceTaskFile("Pack/Movie.mkv", 10),))[0]
    repeated = identify_task_units((SourceTaskFile("Pack/Movie.mkv", 10),))[0]
    changed = identify_task_units((SourceTaskFile("Pack/Movie.mkv", 11),))[0]

    assert first.normalized_unit_key == repeated.normalized_unit_key
    assert first.normalized_unit_key != changed.normalized_unit_key
    assert len(first.normalized_unit_key) == 64


@pytest.mark.parametrize(
    "path",
    (
        "../Movie.mkv",
        "/Movie.mkv",
        "C:/Movie.mkv",
        "Pack//Movie.mkv",
        "Pack/./Movie.mkv",
        "a\x00b.mkv",
    ),
)
def test_unsafe_source_unit_paths_are_rejected(path: str) -> None:
    with pytest.raises(DomainViolation) as raised:
        SourceTaskFile(path, 1)

    assert raised.value.code is ErrorCode.SOURCE_UNIT_INVALID


def test_windows_separator_is_normalized_without_allowing_traversal() -> None:
    source = SourceTaskFile(r"Pack\Season 01\Show.S01E01.mkv", 10)

    assert source.relative_path == "Pack/Season 01/Show.S01E01.mkv"


def test_duplicate_logical_paths_are_rejected_after_normalization() -> None:
    with pytest.raises(DomainViolation) as raised:
        identify_task_units(
            (
                SourceTaskFile("Pack/Movie.mkv", 10),
                SourceTaskFile(r"Pack\Movie.mkv", 10),
            )
        )

    assert raised.value.code is ErrorCode.SOURCE_UNIT_INVALID


def test_case_distinct_posix_paths_keep_distinct_unit_identity() -> None:
    units = identify_task_units(
        (
            SourceTaskFile("Pack/Movie.mkv", 10),
            SourceTaskFile("Pack/movie.mkv", 10),
        )
    )

    assert len(units) == 2
    assert units[0].normalized_unit_key != units[1].normalized_unit_key


def test_zero_length_and_unrecognized_disc_segments_are_not_guessed_as_units() -> None:
    units = identify_task_units(
        (
            SourceTaskFile("BDMV/STREAM/00001.m2ts", 1000),
            SourceTaskFile("Pack/empty.mkv", 0),
        )
    )

    assert units == ()
