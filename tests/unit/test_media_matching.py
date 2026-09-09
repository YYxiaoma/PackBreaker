from backend.app.domain.media_matching import (
    EpisodeKind,
    media_file_token_signature,
    parse_media_name,
)


def test_release_name_extracts_known_tokens_without_truncating_title() -> None:
    result = parse_media_name(
        "Spider-Man.No.Way.Home.2021.2160p.WEB-DL.H.265.HDR10.Atmos",
        release_group="Example",
    )

    assert result.title_tokens == ("spider", "man", "no", "way", "home")
    assert result.year == 2021
    assert result.resolution == "2160p"
    assert result.release_source == "web-dl"
    assert result.codec == "hevc"
    assert result.hdr == "hdr10"
    assert result.audio == "atmos"
    assert result.release_group == "example"


def test_episode_forms_are_modeled_separately() -> None:
    single = parse_media_name("Show.S01E02.1080p")
    ranged = parse_media_name("Show.S01E01-E03.1080p")
    episode = parse_media_name("Show.EP02.1080p")
    absolute = parse_media_name("Show.ABS12.1080p")
    special = parse_media_name("Show.S00E03.1080p")

    assert single.episode is not None and single.episode.kind is EpisodeKind.SEASON_EPISODE
    assert ranged.episode is not None and ranged.episode.kind is EpisodeKind.SEASON_RANGE
    assert episode.episode is not None and episode.episode.kind is EpisodeKind.EPISODE
    assert absolute.episode is not None and absolute.episode.kind is EpisodeKind.ABSOLUTE
    assert special.episode is not None and special.episode.kind is EpisodeKind.SPECIALS


def test_bare_number_is_not_guessed_as_absolute_episode() -> None:
    result = parse_media_name("Show 12 Final")

    assert result.episode is None
    assert result.title_tokens == ("show", "12", "final")


def test_external_ids_are_normalized() -> None:
    result = parse_media_name("Movie 2024 tt1234567 Douban-1295644")

    assert {(item.namespace, item.value) for item in result.external_ids} == {
        ("imdb", "tt1234567"),
        ("douban", "1295644"),
    }


def test_explicit_language_release_tokens_are_normalized() -> None:
    simplified = parse_media_name("Movie.2024.1080p.CHS")
    english = parse_media_name("Movie.2024.1080p.English")

    assert simplified.language == "zh-cn"
    assert english.language == "en"
    assert simplified.title_tokens == ("movie",)


def test_file_token_signature_canonicalizes_known_release_variants() -> None:
    left = media_file_token_signature("Movie.2160p.WEB-DL.H.265.mkv")
    right = media_file_token_signature("movie.4K.webdl.HEVC.mkv")

    assert left == right
