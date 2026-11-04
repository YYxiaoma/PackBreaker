from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from sqlalchemy import select

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter, SiteConfigReference
from backend.app.application.unpack_templates import UnpackTemplateService
from backend.app.domain.media_matching import MediaFileSummary, parse_media_name
from backend.app.domain.site_adapter import TorrentDetails
from backend.app.domain.site_search import CandidateMeta
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    Site,
    UnpackTemplateSeed,
    UnpackTemplateVersion,
    UnpackTorrentMetadata,
    new_uuid,
)


class _FakeAdapter:
    def __init__(
        self,
        *,
        fail_once_ids: set[str] | None = None,
        display_names: dict[str, str] | None = None,
        retryable_errors_once: dict[str, SiteAdapterError] | None = None,
    ) -> None:
        self.requested_ids: list[str] = []
        self.fail_once_ids = set(fail_once_ids or ())
        self.failed_once_ids: set[str] = set()
        self.display_names = dict(display_names or {})
        self.retryable_errors_once = dict(retryable_errors_once or {})

    async def capabilities(self) -> NoReturn:
        raise AssertionError("template import tests must only call fetch_details")

    async def test_connection(self) -> NoReturn:
        raise AssertionError("template import tests must only call fetch_details")

    async def fetch_user_profile(self) -> NoReturn:
        raise AssertionError("template import tests must only call fetch_details")

    async def search(self, query: object) -> NoReturn:
        raise AssertionError("template import tests must only call fetch_details")

    async def fetch_torrent(self, torrent_id: str) -> NoReturn:
        raise AssertionError("template import tests must only call fetch_details")

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        self.requested_ids.append(torrent_id)
        if torrent_id in self.fail_once_ids and torrent_id not in self.failed_once_ids:
            self.failed_once_ids.add(torrent_id)
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "synthetic transient detail failure")
        if torrent_id in self.retryable_errors_once and torrent_id not in self.failed_once_ids:
            self.failed_once_ids.add(torrent_id)
            raise self.retryable_errors_once[torrent_id]
        size_bytes = 1234
        file_summary = (MediaFileSummary(f"{torrent_id}.mkv", size_bytes),)
        display_name = self.display_names.get(torrent_id, f"Sample Film {torrent_id} 1080p")
        descriptor = parse_media_name(
            display_name,
            total_size=size_bytes,
            files=file_summary,
        )
        return TorrentDetails(
            CandidateMeta(
                site_id="mteam",
                torrent_id=torrent_id,
                display_name=display_name,
                descriptor=descriptor,
                total_size=size_bytes,
                file_summary=file_summary,
            )
        )


class _FakeSiteService:
    def __init__(self, binding: EnabledSiteAdapter) -> None:
        self.binding = binding

    def adapter_for_config(self, site_config_id: str) -> EnabledSiteAdapter:
        assert site_config_id == self.binding.config_id
        return self.binding

    def config_references_for_site_keys(
        self, site_keys: tuple[str, ...]
    ) -> tuple[SiteConfigReference, ...]:
        if self.binding.site_id not in site_keys:
            return ()
        return (
            SiteConfigReference(
                config_id=self.binding.config_id,
                config_version=self.binding.config_version,
                site_id=self.binding.site_id,
                enabled=True,
            ),
        )


def test_template_import_creates_verified_version_and_appends_immutably(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "template-library.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    site_id = new_uuid()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="Template library test site",
                type="MTEAM",
                base_url="https://example.invalid",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    adapter = _FakeAdapter()
    binding = EnabledSiteAdapter(
        config_id=site_id,
        config_version=1,
        site_id="mteam",
        adapter=adapter,
        request_timeout_seconds=15,
        search_interval_seconds=0,
    )
    site_service = _FakeSiteService(binding)
    service = UnpackTemplateService(session_factory)
    try:
        first_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-123", "seed-123"),
            name="Sample movie template",
            source_label="synthetic test",
            seed_mappings=(
                {
                    "torrent_id": "seed-123",
                    "collection_root_name": (
                        "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
                    ),
                    "relative_directory": "惊魂记.Psycho.1960.BluRay.FRDS",
                    "media_title": "惊魂记.Psycho.1960.BluRay.FRDS",
                    "confidence_bps": 9998,
                    "source_execution_id": "execution-source-1",
                },
            ),
        )
        assert first_job.total_count == 1
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]

        completed_first = service.get_import_job(first_job.id)
        assert completed_first.status == "COMPLETED", completed_first.errors
        assert completed_first.success_count == 1
        assert completed_first.failure_count == 0

        first_template = service.list_templates(site_service)  # type: ignore[arg-type]
        assert len(first_template) == 1
        template = first_template[0]
        assert template.status == "READY"
        assert template.current_version == 1
        assert template.seed_count == 1
        assert template.site_keys == ("mteam",)
        assert template.site_ids == (site_id,)
        assert template.mapped_media_count == 1
        assert len(template.content_digest) == 64
        with session_factory() as session:
            seed = session.scalar(select(UnpackTemplateSeed))
            assert seed is not None
            assert seed.mapping_rules["source_mappings"] == [
                {
                    "collection_root_name": (
                        "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
                    ),
                    "relative_directory": "惊魂记.Psycho.1960.BluRay.FRDS",
                    "media_title": "惊魂记.Psycho.1960.BluRay.FRDS",
                    "confidence_bps": 9998,
                    "source_execution_id": "execution-source-1",
                    "mapping_status": "SUGGESTED",
                }
            ]

        second_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-456",),
            template_id=template.id,
        )
        assert second_job.template_version == 2
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]

        completed_second = service.get_import_job(second_job.id)
        assert completed_second.status == "COMPLETED"
        assert completed_second.success_count == 1
        assert completed_second.failure_count == 0
        current_template = service.list_templates(site_service)[0]  # type: ignore[arg-type]
        assert current_template.current_version == 2
        assert current_template.status == "READY"
        assert current_template.seed_count == 2
        assert current_template.mapped_media_count == 1

        mapping_update_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-123",),
            template_id=template.id,
            seed_mappings=(
                {
                    "torrent_id": "seed-123",
                    "collection_root_name": "Another.Collection",
                    "relative_directory": "Movie 2024",
                    "media_title": "Movie 2024",
                    "confidence_bps": 9000,
                },
            ),
        )
        assert mapping_update_job.template_version == 3
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        completed_mapping_update = service.get_import_job(mapping_update_job.id)
        assert completed_mapping_update.status == "COMPLETED"
        current_template = service.list_templates(site_service)[0]  # type: ignore[arg-type]
        assert current_template.current_version == 3
        assert current_template.seed_count == 2
        assert current_template.mapped_media_count == 2
        assert adapter.requested_ids == ["seed-123", "seed-456", "seed-123"]

        other_site_id = new_uuid()
        other_binding = EnabledSiteAdapter(
            config_id=other_site_id,
            config_version=1,
            site_id="other-site",
            adapter=adapter,
            request_timeout_seconds=15,
            search_interval_seconds=0,
        )
        with pytest.raises(ApplicationError) as cross_site_error:
            service.create_import_job(
                site_service=_FakeSiteService(other_binding),  # type: ignore[arg-type]
                site_config_id=other_site_id,
                torrent_ids=("cross-site-seed",),
                template_id=template.id,
            )
        assert cross_site_error.value.code == "UNPACK_TEMPLATE_CONFLICT"

        with pytest.raises(ApplicationError) as unsafe_path_error:
            service.create_import_job(
                site_service=site_service,  # type: ignore[arg-type]
                site_config_id=site_id,
                torrent_ids=("unsafe-seed",),
                name="Unsafe mapping template",
                seed_mappings=(
                    {
                        "torrent_id": "unsafe-seed",
                        "collection_root_name": "Collection",
                        "relative_directory": "../outside",
                        "media_title": "Outside",
                    },
                ),
            )
        assert unsafe_path_error.value.code == "UNPACK_TEMPLATE_INVALID"

        with pytest.raises(ApplicationError) as nul_path_error:
            service.create_import_job(
                site_service=site_service,  # type: ignore[arg-type]
                site_config_id=site_id,
                torrent_ids=("nul-seed",),
                name="NUL mapping template",
                seed_mappings=(
                    {
                        "torrent_id": "nul-seed",
                        "collection_root_name": "Collection",
                        "relative_directory": "Movie\x00/escape",
                        "media_title": "NUL",
                    },
                ),
            )
        assert nul_path_error.value.code == "UNPACK_TEMPLATE_INVALID"

        with session_factory() as session:
            versions = session.scalars(
                select(UnpackTemplateVersion)
                .where(UnpackTemplateVersion.template_id == template.id)
                .order_by(UnpackTemplateVersion.version)
            ).all()
            assert [(item.version, item.status, item.seed_count) for item in versions] == [
                (1, "READY", 1),
                (2, "READY", 2),
                (3, "READY", 2),
            ]
            metadata = session.scalars(select(UnpackTorrentMetadata)).all()
            assert len(metadata) == 2
            assert all(item.verification_status == "DETAILS_VERIFIED" for item in metadata)
            assert all(len(item.metadata_digest) == 64 for item in metadata)
    finally:
        engine.dispose()


def test_template_import_uses_adapter_managed_timeout_without_outer_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_sqlite_engine(tmp_path / "template-library-timeout.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    site_id = new_uuid()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="Template timeout test site",
                type="MTEAM",
                base_url="https://example.invalid",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    adapter = _FakeAdapter()
    binding = EnabledSiteAdapter(
        config_id=site_id,
        config_version=1,
        site_id="mteam",
        adapter=adapter,
        request_timeout_seconds=15,
        search_interval_seconds=0,
    )
    site_service = _FakeSiteService(binding)
    service = UnpackTemplateService(session_factory)

    def reject_outer_timeout(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("template import must not override ReliableSiteAdapter timeout policy")

    monkeypatch.setattr(
        "backend.app.application.unpack_templates.asyncio.wait_for", reject_outer_timeout
    )
    try:
        job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-adapter-timeout",),
            name="Template timeout test",
        )
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        completed = service.get_import_job(job.id)
        assert completed.status == "COMPLETED", completed.errors
        template = service.list_templates(site_service)[0]  # type: ignore[arg-type]
        assert template.status == "READY"
    finally:
        engine.dispose()


def test_incomplete_template_can_retry_failed_seed_without_losing_mappings(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "template-library-retry.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    site_id = new_uuid()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="Template retry test site",
                type="MTEAM",
                base_url="https://example.invalid",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    adapter = _FakeAdapter(fail_once_ids={"seed-retry"})
    binding = EnabledSiteAdapter(
        config_id=site_id,
        config_version=1,
        site_id="mteam",
        adapter=adapter,
        request_timeout_seconds=15,
        search_interval_seconds=0,
    )
    site_service = _FakeSiteService(binding)
    service = UnpackTemplateService(session_factory)
    mappings: tuple[dict[str, Any], ...] = (
        {
            "torrent_id": "seed-keep",
            "collection_root_name": "Collection",
            "relative_directory": "Film.Keep.2020",
            "media_title": "Film Keep 2020",
            "confidence_bps": 10_000,
        },
        {
            "torrent_id": "seed-retry",
            "collection_root_name": "Collection",
            "relative_directory": "Film.Retry.2021",
            "media_title": "Film Retry 2021",
            "confidence_bps": 10_000,
        },
    )
    try:
        first_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-keep", "seed-retry"),
            name="Template retry test",
            seed_mappings=mappings,
        )
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]

        incomplete_job = service.get_import_job(first_job.id)
        assert incomplete_job.status == "COMPLETED_WITH_ERRORS"
        assert incomplete_job.success_count == 1
        assert incomplete_job.failure_count == 1
        template = service.list_templates(site_service)[0]  # type: ignore[arg-type]
        assert template.status == "INCOMPLETE"
        assert template.current_version == 1
        assert template.seed_count == 1

        retry_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-retry",),
            template_id=first_job.template_id,
            seed_mappings=(mappings[1],),
        )
        assert retry_job.template_version == 2
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]

        completed_retry = service.get_import_job(retry_job.id)
        assert completed_retry.status == "COMPLETED"
        assert completed_retry.success_count == 1
        assert completed_retry.failure_count == 0
        template = service.list_templates(site_service)[0]  # type: ignore[arg-type]
        assert template.status == "READY"
        assert template.current_version == 2
        assert template.seed_count == 2
        assert template.mapped_media_count == 2
        assert adapter.requested_ids == ["seed-keep", "seed-retry", "seed-retry"]

        with session_factory() as session:
            version = session.scalar(
                select(UnpackTemplateVersion)
                .where(UnpackTemplateVersion.template_id == first_job.template_id)
                .where(UnpackTemplateVersion.version == 2)
            )
            assert version is not None
            seeds = session.execute(
                select(UnpackTemplateSeed, UnpackTorrentMetadata)
                .join(
                    UnpackTorrentMetadata,
                    UnpackTorrentMetadata.id == UnpackTemplateSeed.metadata_id,
                )
                .where(UnpackTemplateSeed.template_version_id == version.id)
            ).all()
            retry_seed = next(
                (seed for seed, metadata in seeds if metadata.remote_torrent_id == "seed-retry"),
                None,
            )
            assert retry_seed is not None
            assert retry_seed.mapping_rules["source_mappings"] == [
                {
                    "collection_root_name": "Collection",
                    "relative_directory": "Film.Retry.2021",
                    "media_title": "Film Retry 2021",
                    "confidence_bps": 10_000,
                    "source_execution_id": None,
                    "mapping_status": "SUGGESTED",
                }
            ]
    finally:
        engine.dispose()


def test_template_import_infers_only_a_confident_safe_movie_directory_mapping(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "isolated-data"
    root_relative = Path("downloads2") / "待刮削" / "电影" / "CollectionRoot"
    collection_root = tmp_path / "approved-media-root" / "CollectionRoot"
    movie_directory = collection_root / "Film.2020.BluRay.1080p.x265-FRDS"
    movie_directory.mkdir(parents=True)
    (movie_directory / "Film.2020.mkv").write_bytes(b"synthetic-test-file")
    collection_directory = collection_root / "星球大战合集.BluRay.1080p.x265"
    collection_directory.mkdir(parents=True)
    star_wars_file = collection_directory / (
        "Star.Wars.Episode.IX.The.Rise.of.Skywalker.2019.UHD.BluRay.mkv"
    )
    star_wars_file.write_bytes(b"synthetic-star-wars-file")
    hachi_directory = collection_root / (
        "忠犬八公的故事.Hachi.A.Dog'sTale.2009.BluRay.1080p.x265.10bit.MNHD-FRDS"
    )
    hachi_directory.mkdir(parents=True)
    hachi_file = hachi_directory / ("Hachi.A.Dog'sTale.2009.BluRay.1080p.x265.10bit.MNHD-FRDS.mkv")
    hachi_file.write_bytes(b"synthetic-hachi-file")
    lock_stock_directory = collection_root / (
        "两杆大烟枪.Lock.Stock.and.Two.Smoking.Barrels.BluRay.1080p.x265.10bit.MNHD-FRDS"
    )
    lock_stock_directory.mkdir(parents=True)
    (
        lock_stock_directory
        / "Lock.Stock.and.Two.Smoking.Barrels.BluRay.1080p.x265.10bit.MNHD-FRDS.mkv"
    ).write_bytes(b"synthetic-lock-stock-file")
    edward_directory = collection_root / (
        "剪刀手爱德华.Edward.Scissorhands.25th.Anniversary.Remastered.Edition.BluRay.1080p.x265.10bit.2Audios.MNHD-FRDS"
    )
    edward_directory.mkdir(parents=True)
    edward_file = edward_directory / (
        "Edward.Scissorhands.25th.Anniversary.Remastered.Edition."
        "BluRay.1080p.x265.10bit.2Audios.MNHD-FRDS.mkv"
    )
    edward_file.write_bytes(b"synthetic-edward-file")

    engine = create_sqlite_engine(tmp_path / "template-library-inferred-map.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    site_id = new_uuid()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="Template inferred mapping test site",
                type="MTEAM",
                base_url="https://example.invalid",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    adapter = _FakeAdapter(display_names={"seed-map": "Film 2020 BluRay 1080p x265-FRDS"})
    binding = EnabledSiteAdapter(
        config_id=site_id,
        config_version=1,
        site_id="mteam",
        adapter=adapter,
        request_timeout_seconds=15,
        search_interval_seconds=0,
    )
    site_service = _FakeSiteService(binding)
    service = UnpackTemplateService(
        session_factory, data_dir=data_dir, mapping_root=collection_root
    )
    try:
        star_wars_mapping = service._infer_source_mappings(
            {
                "collection_root_name": "CollectionRoot",
                "collection_root_relative_path": root_relative.as_posix(),
            },
            "Star Wars Episode IX The Rise of Skywalker 2019 UHD BluRay",
        )
        assert len(star_wars_mapping) == 1
        assert (
            star_wars_mapping[0]["relative_directory"]
            == collection_directory.relative_to(collection_root).as_posix()
        )
        assert star_wars_mapping[0]["media_title"] == star_wars_file.stem
        hachi_mapping = service._infer_source_mappings(
            {
                "collection_root_name": "CollectionRoot",
                "collection_root_relative_path": root_relative.as_posix(),
            },
            "Hachi A DogsTale 2009 BluRay 1080p x265 10bit MNHD-FRDS [ 50% ]",
        )
        assert len(hachi_mapping) == 1
        assert (
            hachi_mapping[0]["relative_directory"]
            == hachi_directory.relative_to(collection_root).as_posix()
        )
        assert hachi_mapping[0]["media_title"] == hachi_directory.name
        assert hachi_mapping[0]["confidence_bps"] >= 9_000
        for source_title, expected_directory in (
            (
                "Lock Stock and Two Smoking Barrels BluRay 1080p x265 10bit MNHD-FRDS [ 50% ]",
                lock_stock_directory,
            ),
            (
                "Edward Scissorhands 25th Anniversary Remastered Edition "
                "BluRay 1080p x265 10bit 2Audios MNHD-FRDS [ 50% ]",
                edward_directory,
            ),
        ):
            mapping = service._infer_source_mappings(
                {
                    "collection_root_name": "CollectionRoot",
                    "collection_root_relative_path": root_relative.as_posix(),
                },
                source_title,
            )
            assert len(mapping) == 1
            assert (
                mapping[0]["relative_directory"]
                == expected_directory.relative_to(collection_root).as_posix()
            )
            assert mapping[0]["mapping_status"] == "SUGGESTED"
            assert mapping[0]["confidence_bps"] >= 9_000
        job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-map",),
            name="Template inferred mapping test",
        )
        with session_factory() as session:
            version = session.scalar(
                select(UnpackTemplateVersion)
                .where(UnpackTemplateVersion.template_id == job.template_id)
                .where(UnpackTemplateVersion.version == job.template_version)
            )
            assert version is not None
            version.source_provenance = {
                **dict(version.source_provenance or {}),
                "collection_root_name": "CollectionRoot",
                "collection_root_relative_path": root_relative.as_posix(),
            }
            session.commit()

        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        completed = service.get_import_job(job.id)
        assert completed.status == "COMPLETED", completed.errors
        with session_factory() as session:
            seed = session.scalar(select(UnpackTemplateSeed))
            assert seed is not None
            mappings = seed.mapping_rules["source_mappings"]
            assert len(mappings) == 1
            assert mappings[0]["collection_root_name"] == "CollectionRoot"
            assert (
                mappings[0]["relative_directory"]
                == movie_directory.relative_to(collection_root).as_posix()
            )
            assert mappings[0]["media_title"] == movie_directory.name
            assert mappings[0]["mapping_status"] == "SUGGESTED"
            assert mappings[0]["confidence_bps"] >= 9_000
    finally:
        engine.dispose()


@pytest.mark.parametrize("transient_code", ["SITE_RATE_LIMITED", "SITE_CIRCUIT_OPEN"])
def test_template_import_retries_transient_site_error_without_advancing_cursor(
    tmp_path: Path, transient_code: str
) -> None:
    engine = create_sqlite_engine(tmp_path / "template-library-rate-limit.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    site_id = new_uuid()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="Template rate limit test site",
                type="MTEAM",
                base_url="https://example.invalid",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    adapter = _FakeAdapter(
        retryable_errors_once={
            "seed-rate-limit": SiteAdapterError(
                transient_code,
                "synthetic transient site error",
                retryable=True,
                retry_after_seconds=0.05,
            )
        }
    )
    binding = EnabledSiteAdapter(
        config_id=site_id,
        config_version=1,
        site_id="mteam",
        adapter=adapter,
        request_timeout_seconds=15,
        search_interval_seconds=0,
    )
    site_service = _FakeSiteService(binding)
    service = UnpackTemplateService(session_factory)
    try:
        job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("seed-rate-limit",),
            name="Template rate limit retry",
        )

        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        waiting = service.get_import_job(job.id)
        assert waiting.status == "RUNNING"
        assert waiting.processed_count == 0
        assert waiting.success_count == 0
        assert waiting.failure_count == 0
        assert waiting.errors == ()

        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        completed = service.get_import_job(job.id)
        assert completed.status == "COMPLETED"
        assert completed.processed_count == 1
        assert completed.success_count == 1
        assert completed.failure_count == 0
        assert adapter.requested_ids == ["seed-rate-limit", "seed-rate-limit"]
    finally:
        engine.dispose()


def test_template_mapping_normalizes_site_prefix_comment_suffix_and_bilingual_aliases(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "isolated-data"
    root_relative = Path("downloads2") / "待刮削" / "电影" / "CollectionRoot"
    collection_root = tmp_path / "approved-media-root" / "CollectionRoot"
    taare_directory = collection_root / (
        "地球上的星星.Taare.Zameen.Par.2007.BluRay.1080p.x265.10bit.MNHD-FRDS"
    )
    taare_directory.mkdir(parents=True)
    (taare_directory / "Taare.Zameen.Par.2007.BluRay.1080p.x265.10bit.MNHD-FRDS.mkv").write_bytes(
        b"synthetic-taare-file"
    )
    godzilla_directory = collection_root / (
        "哥斯拉-1.0.Godzilla.Minus.One.2023.BluRay.1080p.x265.10bit.DDP7.1.MNHD-FRDS"
    )
    godzilla_directory.mkdir(parents=True)
    godzilla_file = godzilla_directory / (
        "Godzilla.Minus.One.2023.BluRay.1080p.x265.10bit.DDP7.1.MNHD-FRDS.mkv"
    )
    godzilla_file.write_bytes(b"synthetic-godzilla-file")

    engine = create_sqlite_engine(tmp_path / "template-library-normalized-map.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = UnpackTemplateService(
        session_factory, data_dir=data_dir, mapping_root=collection_root
    )
    provenance = {
        "collection_root_name": "CollectionRoot",
        "collection_root_relative_path": root_relative.as_posix(),
    }
    try:
        taare = service._infer_source_mappings(
            provenance,
            "[HDFans] 地球上的星星 Taare Zameen Par 2007 BluRay 1080p "
            "x265.10bit MNHD-FRDS [ 50% ] 用户评论",
        )
        assert len(taare) == 1
        assert (
            taare[0]["relative_directory"]
            == taare_directory.relative_to(collection_root).as_posix()
        )

        godzilla = service._infer_source_mappings(
            provenance,
            "Godzilla Minus One 2023 BluRay 1080p x265 10bit DDP7.1 MNHD-FRDS [ 50% ]",
        )
        assert len(godzilla) == 1
        assert (
            godzilla[0]["relative_directory"]
            == godzilla_directory.relative_to(collection_root).as_posix()
        )
    finally:
        engine.dispose()


def test_template_mapping_expands_only_unambiguous_collection_roots_to_movie_folders(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "isolated-data"
    root_relative = Path("downloads2") / "待刮削" / "电影" / "CollectionRoot"
    collection_root = tmp_path / "approved-media-root" / "CollectionRoot"
    hp_parent = collection_root / "哈利波特魔法大包.Harry.Potter.Film.Collection"
    hp_names = (
        "Harry.Potter.and.the.Sorcerers.Stone.2001.BluRay.1080p.x265",
        "Harry.Potter.and.the.Chamber.of.Secrets.2002.BluRay.1080p.x265",
    )
    for name in hp_names:
        folder = hp_parent / name
        folder.mkdir(parents=True)
        (folder / f"{name}.mkv").write_bytes(b"synthetic-harry-potter-file")

    saw_parent = collection_root / "Saw.2004-2017.Collection.BluRay.1080p.x265"
    duplicate_saw_parent = collection_root / (
        "电锯惊魂合集.Saw.2004-2017.Collection.BluRay.1080p.x265"
    )
    for parent in (saw_parent, duplicate_saw_parent):
        for name in ("Saw.2004.BluRay.1080p.x265", "Saw.II.2005.BluRay.1080p.x265"):
            folder = parent / name
            folder.mkdir(parents=True)
            (folder / f"{name}.mkv").write_bytes(b"synthetic-saw-file")

    rocky_flat_pack = collection_root / "洛奇合集.Rocky.1976-2015.1080p.BluRay.x265.10bit.MNHD-FRDS"
    rocky_flat_pack.mkdir(parents=True)
    (rocky_flat_pack / "Rocky.1976.BluRay.mkv").write_bytes(b"synthetic-rocky-one")
    (rocky_flat_pack / "Rocky.II.1979.BluRay.mkv").write_bytes(b"synthetic-rocky-two")

    engine = create_sqlite_engine(tmp_path / "template-library-collection-map.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = UnpackTemplateService(
        session_factory, data_dir=data_dir, mapping_root=collection_root
    )
    provenance = {
        "collection_root_name": "CollectionRoot",
        "collection_root_relative_path": root_relative.as_posix(),
    }
    try:
        harry_potter = service._infer_source_mappings(
            provenance, "Harry Potter Film Collection 2001-2011 BluRay 1080p x265"
        )
        assert {item["relative_directory"] for item in harry_potter} == {
            (hp_parent / name).relative_to(collection_root).as_posix() for name in hp_names
        }
        assert all(item["confidence_bps"] >= 9_000 for item in harry_potter)

        saw = service._infer_source_mappings(
            provenance, "Saw 2004-2017 Collection BluRay 1080p x265"
        )
        # The same collection exists twice with identical movie folders; do not
        # silently choose whichever root sorts first.
        assert saw == []

        rocky = service._infer_source_mappings(
            provenance, "Rocky 1976-2015 BluRay 1080p x265 10bit"
        )
        assert rocky == []
        flat_collection_single_movie = service._infer_source_mappings(
            provenance, "Rocky 1976 BluRay 1080p x265"
        )
        # A file stem inside a flat multi-film pack must not map to that pack folder.
        assert flat_collection_single_movie == []
    finally:
        engine.dispose()


def test_collection_mapping_supports_single_token_title_with_detailed_movie_folders(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "isolated-data"
    collection_root = tmp_path / "approved-media-root" / "CollectionRoot"
    rocky_parent = collection_root / "Rocky.1976-2015.Collection"
    movie_names = (
        "Rocky.1976.BluRay.1080p.x265",
        "Rocky.II.1979.BluRay.1080p.x265",
        "Rocky.III.1982.BluRay.1080p.x265",
        "Creed.2015.BluRay.1080p.x265",
    )
    for name in movie_names:
        folder = rocky_parent / name
        folder.mkdir(parents=True)
        (folder / f"{name}.mkv").write_bytes(b"synthetic-rocky-movie")

    engine = create_sqlite_engine(tmp_path / "template-library-rocky-nested.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = UnpackTemplateService(
        session_factory, data_dir=data_dir, mapping_root=collection_root
    )
    provenance = {
        "collection_root_name": "CollectionRoot",
        "collection_root_relative_path": "downloads2/待刮削/电影/CollectionRoot",
    }
    try:
        mappings = service._infer_source_mappings(
            provenance, "Rocky 1976-2015 1080p BluRay x265 10bit"
        )
        assert {item["relative_directory"] for item in mappings} == {
            (rocky_parent / name).relative_to(collection_root).as_posix() for name in movie_names
        }
        assert all(item["relative_directory"].count("/") == 1 for item in mappings)
    finally:
        engine.dispose()


def test_template_mapping_prefers_unique_exact_release_order_and_keeps_duplicates_ambiguous(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "isolated-data"
    collection_root = tmp_path / "approved-media-root" / "CollectionRoot"
    exact_harakiri = collection_root / (
        "切腹.Harakiri.aka.Seppuku.1962.1080p.Bluray.x265.10bit.MNHD-FRDS"
    )
    reordered_harakiri = collection_root / (
        "切腹.Harakiri.aka.Seppuku.1962.BluRay.1080p.x265.10bit.MNHD-FRDS"
    )
    exact_harakiri.mkdir(parents=True)
    reordered_harakiri.mkdir(parents=True)
    (exact_harakiri / f"{exact_harakiri.name}.mkv").write_bytes(b"synthetic-harakiri-exact")
    (reordered_harakiri / f"{reordered_harakiri.name}.mkv").write_bytes(
        b"synthetic-harakiri-reordered"
    )
    bohemian = collection_root / "Bohemian.Rhapsody.2018.BluRay.1080p.x265.10bit.MNHD-FRDS"
    localized_bohemian = collection_root / (
        "波西米亚狂想曲.Bohemian.Rhapsody.2018.BluRay.1080p.x265.10bit.MNHD-FRDS"
    )
    for folder in (bohemian, localized_bohemian):
        folder.mkdir(parents=True)
        (folder / f"{folder.name}.mkv").write_bytes(b"synthetic-bohemian")

    engine = create_sqlite_engine(tmp_path / "template-library-exact-order.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = UnpackTemplateService(
        session_factory, data_dir=data_dir, mapping_root=collection_root
    )
    provenance = {
        "collection_root_name": "CollectionRoot",
        "collection_root_relative_path": "downloads2/待刮削/电影/CollectionRoot",
    }
    try:
        harakiri = service._infer_source_mappings(
            provenance,
            "Harakiri aka Seppuku 1962 1080p Bluray x265 10bit MNHD-FRDS [ 50% ]",
        )
        assert len(harakiri) == 1
        assert (
            harakiri[0]["relative_directory"]
            == exact_harakiri.relative_to(collection_root).as_posix()
        )

        ambiguous_bohemian = service._infer_source_mappings(
            provenance, "Bohemian Rhapsody 2018 BluRay 1080p x265 10bit MNHD-FRDS"
        )
        assert ambiguous_bohemian == []
    finally:
        engine.dispose()


def test_collection_parent_with_children_is_not_a_single_movie_mapping(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "isolated-data"
    collection_root = tmp_path / "approved-media-root" / "CollectionRoot"
    parent = collection_root / "星球大战合集.2019.BluRay.1080p.x265"
    parent.mkdir(parents=True)
    (parent / "Star.Wars.Episode.IX.The.Rise.of.Skywalker.2019.UHD.BluRay.mkv").write_bytes(
        b"synthetic-star-wars-episode-nine"
    )
    child = parent / "星球大战4.Star.Wars.Episode.IV.1977.BluRay.1080p.x265"
    child.mkdir()
    (child / f"{child.name}.mkv").write_bytes(b"synthetic-star-wars-episode-four")

    engine = create_sqlite_engine(tmp_path / "template-library-collection-parent.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = UnpackTemplateService(
        session_factory, data_dir=data_dir, mapping_root=collection_root
    )
    provenance = {
        "collection_root_name": "CollectionRoot",
        "collection_root_relative_path": "downloads2/待刮削/电影/CollectionRoot",
    }
    try:
        mappings = service._infer_source_mappings(
            provenance, "Star Wars Episode IX The Rise of Skywalker 2019 UHD BluRay"
        )
        assert mappings == []
    finally:
        engine.dispose()


def test_template_reimport_preserves_metainfo_paths_and_existing_provenance(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "template-library-metadata-merge.db")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    site_id = new_uuid()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="Template metadata merge test site",
                type="MTEAM",
                base_url="https://example.invalid",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    adapter = _FakeAdapter()
    binding = EnabledSiteAdapter(
        config_id=site_id,
        config_version=1,
        site_id="mteam",
        adapter=adapter,
        request_timeout_seconds=15,
        search_interval_seconds=0,
    )
    site_service = _FakeSiteService(binding)
    service = UnpackTemplateService(session_factory)
    first_mapping = {
        "torrent_id": "collection-699527",
        "collection_root_name": "Marvel.Collection",
        "relative_directory": "01.Iron.Man.2008",
        "media_title": "Iron Man 2008 UHD REMUX",
        "confidence_bps": 10_000,
    }
    second_mapping = {
        "torrent_id": "collection-699527",
        "collection_root_name": "Marvel.Collection",
        "relative_directory": "16.Spider-Man.Homecoming.2017",
        "media_title": "Spider-Man Homecoming 2017 UHD REMUX",
        "confidence_bps": 10_000,
    }
    try:
        first_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("collection-699527",),
            name="Marvel collection template",
            seed_mappings=(first_mapping,),
        )
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        assert service.get_import_job(first_job.id).status == "COMPLETED"

        with session_factory() as session:
            metadata = session.scalar(select(UnpackTorrentMetadata))
            assert metadata is not None
            metadata.info_hash = "a" * 40
            metadata.file_summary = [
                {
                    "basename": "Iron.Man.2008.mkv",
                    "length": 1234,
                    "path": "Marvel.Collection/01.Iron.Man.2008/Iron.Man.2008.mkv",
                }
            ]
            descriptor = dict(metadata.descriptor)
            descriptor["files"] = [{"basename": "Iron.Man.2008.mkv", "length": 1234}]
            metadata.descriptor = descriptor
            metadata.source_provenance = {
                "source": "site_detail_api+torrent_metainfo",
                "site_key": "mteam",
                "metainfo_digest": "b" * 64,
                "transmission_match": {
                    "downloader_id": "read-only-evidence",
                    "info_hash": "a" * 40,
                },
            }
            session.commit()

        second_job = service.create_import_job(
            site_service=site_service,  # type: ignore[arg-type]
            site_config_id=site_id,
            torrent_ids=("collection-699527",),
            template_id=first_job.template_id,
            seed_mappings=(second_mapping,),
        )
        assert second_job.template_version == 2
        assert asyncio.run(service.process_next_import_item(site_service))  # type: ignore[arg-type]
        assert service.get_import_job(second_job.id).status == "COMPLETED"

        with session_factory() as session:
            metadata = session.scalar(select(UnpackTorrentMetadata))
            assert metadata is not None
            assert metadata.info_hash == "a" * 40
            assert metadata.file_summary == [
                {
                    "basename": "Iron.Man.2008.mkv",
                    "length": 1234,
                    "path": "Marvel.Collection/01.Iron.Man.2008/Iron.Man.2008.mkv",
                }
            ]
            assert metadata.source_provenance["transmission_match"]["info_hash"] == "a" * 40
            assert metadata.source_provenance["metainfo_digest"] == "b" * 64
            assert metadata.source_provenance["details_source"] == "site_detail_api"
            assert metadata.descriptor["files"] == [
                {"basename": "Iron.Man.2008.mkv", "length": 1234}
            ]
            version = session.scalar(
                select(UnpackTemplateVersion).where(
                    UnpackTemplateVersion.template_id == first_job.template_id,
                    UnpackTemplateVersion.version == 2,
                )
            )
            assert version is not None
            seed = session.scalar(
                select(UnpackTemplateSeed).where(
                    UnpackTemplateSeed.template_version_id == version.id
                )
            )
            assert seed is not None
            mapped_directories = {
                item["relative_directory"] for item in seed.mapping_rules["source_mappings"]
            }
            assert mapped_directories == {
                "01.Iron.Man.2008",
                "16.Spider-Man.Homecoming.2017",
            }
    finally:
        engine.dispose()
