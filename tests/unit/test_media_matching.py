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


def test_numeric_title_uses_trailing_release_year() -> None:
    space_odyssey = parse_media_name("2001.A.Space.Odyssey.1968.BluRay.1080p.x265.10bit.MNHD-FRDS")
    nineteen_seventeen = parse_media_name("1917.2019.BluRay.1080p.x265.10bit.MNHD-FRDS")

    assert space_odyssey.year == 1968
    assert space_odyssey.title_tokens[:4] == ("2001", "a", "space", "odyssey")
    assert nineteen_seventeen.year == 2019
    assert nineteen_seventeen.title_tokens[0] == "1917"


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


def test_technical_release_metadata_and_group_are_not_treated_as_title_tokens() -> None:
    source = parse_media_name(
        "Leon.1994.Extended.Remastered.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
        aliases=("这个杀手不太冷",),
    )
    candidate = parse_media_name(
        "这个杀手不太冷 1994 Leon The Professional 1994 "
        "2in1 2160p UHD Blu-ray HEVC Atmos TrueHD 7.1-CrsS"
    )

    assert source.title_tokens == ("leon", "extended", "remastered")
    assert ("leon",) in source.alias_tokens
    assert source.release_group == "frds"
    assert candidate.release_group == "crss"
    assert not {"10bit", "2audio", "mnhd", "frds"} & set(source.title_tokens)
    assert not {"2in1", "uhd", "crss"} & set(candidate.title_tokens)


def test_audio_channel_counts_and_broadcast_tags_are_not_title_tokens() -> None:
    mad_max = parse_media_name("Mad.Max.2.1981.1080p.BluRay.DTS-HD.MA.5.1.x265.10bit.MNHD-FRDS")
    comedy_king = parse_media_name("喜剧之王.1999.1080i.AC3-2Audio.x5.1-H264_HDCTV_翡翠台源码")

    assert mad_max.title_tokens == ("mad", "max", "2")
    assert comedy_king.title_tokens == ("喜剧之王",)
    assert comedy_king.year == 1999
    assert comedy_king.audio == "ac3"
    assert comedy_king.release_source == "hdtv"


def test_release_group_recognizes_watermarks_duplicate_suffixes_and_containers() -> None:
    watermark = parse_media_name("Movie.2024.1080p.WEB-DL.x265.10bit.AC3￡cXcY@FRDS")
    audio_watermark = parse_media_name(
        "Slumdog.Millionaire.2008.1080p.Blu-ray.x265.10bit.DTS-HD￡cXcY@FRDS"
    )
    plain_suffix = parse_media_name("Devils.on.the.Doorstep.2000.DVDrip.720p.x265.10bit.FRDS")
    duplicate_copy = parse_media_name("Aladdin.1992.BluRay.1080p.x265.10bit.4Audios.MNHD-FRDS (1)")
    container_label = parse_media_name("Kill Bill Vol 1 2003 BluRay 1080p x265.10bit MNHD-FRDS mkv")
    uploader_suffix = parse_media_name("Detachment.2011.Blu-Ray.1080p.AC3.x265.10bit-Yumi@FRDS")
    greenotea = parse_media_name("A.Better.Tomorrow.1986.1080p.x265.GREENOTEA")

    assert watermark.release_group == "cxcy@frds"
    assert audio_watermark.release_group == "cxcy@frds"
    assert plain_suffix.release_group == "frds"
    assert duplicate_copy.release_group == "frds"
    assert container_label.release_group == "frds"
    assert uploader_suffix.release_group == "yumi@frds"
    assert greenotea.release_group == "greenotea"
