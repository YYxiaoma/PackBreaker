import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast

import httpx2
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.sites import (
    EnabledSiteAdapter,
    SiteConfigReference,
    SiteRiskControlOutcome,
)
from backend.app.application.unpack_matching import (
    UnpackMatchCoordinator,
    _has_matching_seed_directory_mapping,
    _mapped_template_descriptor,
    _should_hydrate_mapped_template_candidate,
    _should_hydrate_unmapped_template_candidate,
    _source_year_matches_title_range,
    _unique_template_file_match,
)
from backend.app.application.unpack_templates import _descriptor_payload
from backend.app.domain.media_matching import (
    ExternalMediaId,
    MediaFileSummary,
    parse_media_name,
)
from backend.app.domain.site_adapter import SiteAdapter, TorrentPayload
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
)
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus, UnpackTaskKind
from backend.app.domain.unpack_matching import assess_unpack_candidate
from backend.app.infrastructure.adapters.rousi_pro import RousiProCandidateAdapter
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Site,
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    UnpackTemplate,
    UnpackTemplateMatchAcceptance,
    UnpackTemplateSeed,
    UnpackTemplateVersion,
    UnpackTorrentMetadata,
    utc_now,
)


class _FakeSiteAdapter:
    def __init__(
        self,
        candidates: tuple[CandidateMeta, ...] = (),
        *,
        error: SiteAdapterError | None = None,
    ) -> None:
        self._candidates = candidates
        self._error = error
        self.queries: list[SearchQuery] = []

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=True,
            supports_douban_id=True,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return SearchPage(
            site_id="fake-site",
            page=1,
            items=self._candidates,
            has_more=False,
        )


class _FakeSiteProvider:
    def __init__(self, adapter: _FakeSiteAdapter | None) -> None:
        self._adapter = adapter

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        if self._adapter is None:
            return ()
        return (
            EnabledSiteAdapter(
                config_id="site-config-1",
                config_version=7,
                site_id="fake-site",
                adapter=cast(SiteAdapter, self._adapter),
            ),
        )


def _factory(
    *,
    threshold: int,
    filename: str = "Movie.2024.1080p.WEB-DL.x265.tt1234567.mkv",
    size: int = 1000,
    retry_enabled: bool = False,
    max_retries: int = 3,
    auto_match_enabled: bool = True,
) -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="匹配测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": "/data"},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-config-1"],
                output_config={},
                retry_enabled=retry_enabled,
                max_retries=max_retries,
                auto_match_threshold_bps=threshold,
                auto_match_enabled=auto_match_enabled,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecution(
                id="execution-1",
                definition_id="definition-1",
                trigger="MANUAL",
                status=UnpackExecutionStatus.MATCHING.value,
                config_snapshot={
                    "site_ids": ["site-config-1"],
                    "matching": {
                        "auto_match_threshold_bps": threshold,
                        "auto_match_enabled": auto_match_enabled,
                    },
                },
                discovery_complete=True,
                total_count=1,
                matched_auto_count=0,
                review_count=0,
                content_verified_count=0,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=0,
                started_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecutionItem(
                id="item-1",
                execution_id="execution-1",
                source_object_key="source-1",
                source_snapshot={
                    "path": f"/data/{filename}",
                    "relative_path": filename,
                    "size": size,
                    "file_type": "regular",
                },
                media_identity={},
                status=UnpackItemStatus.MATCH_PENDING.value,
                candidate_generation=0,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return factory


def _add_pending_item(
    factory: sessionmaker[Session], *, item_id: str, filename: str = "Another.Movie.2024.mkv"
) -> None:
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExecutionItem(
                id=item_id,
                execution_id="execution-1",
                source_object_key=f"source-{item_id}",
                source_snapshot={
                    "path": f"/data/{filename}",
                    "relative_path": filename,
                    "size": 1000,
                    "file_type": "regular",
                },
                media_identity={},
                status=UnpackItemStatus.MATCH_PENDING.value,
                candidate_generation=0,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def _bencode_for_matching_test(value: object) -> bytes:
    if isinstance(value, int) and not isinstance(value, bool):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_bencode_for_matching_test(item) for item in value) + b"e"
    if isinstance(value, dict):
        entries = sorted(value.items())
        return (
            b"d"
            + b"".join(
                _bencode_for_matching_test(key) + _bencode_for_matching_test(item)
                for key, item in entries
            )
            + b"e"
        )
    raise TypeError(f"unsupported test bencode value: {type(value).__name__}")


def _single_file_torrent_for_matching_test(name: str, length: int) -> bytes:
    return _bencode_for_matching_test(
        {
            b"info": {
                b"length": length,
                b"name": name.encode("utf-8"),
                b"piece length": 16 * 1024,
                b"pieces": b"\x00" * 20,
            }
        }
    )


def _candidate(
    name: str,
    *,
    torrent_id: str = "torrent-1",
    size: int = 1000,
    imdb_id: str = "tt1234567",
) -> CandidateMeta:
    descriptor = parse_media_name(
        f"{name}.{imdb_id}",
        total_size=size,
        files=(MediaFileSummary(f"{name}.mkv", size),),
    )
    return CandidateMeta(
        site_id="fake-site",
        torrent_id=torrent_id,
        display_name=f"{name}.{imdb_id}",
        descriptor=descriptor,
        total_size=size,
        seeders=12,
        file_summary=descriptor.files,
    )


@pytest.mark.parametrize("duplicate_mapping", [False, True])
def test_template_task_uses_unique_saved_mapping_without_querying_site_adapters(
    duplicate_mapping: bool,
) -> None:
    collection_root = "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
    movie_directory = "Movie.2024.1080p.WEB-DL.x265.tt1234567"
    source_filename = f"{movie_directory}.mkv"
    factory = _factory(threshold=9900, filename=source_filename)
    now = utc_now()
    display_name = "Unrelated.Movie.1999.1080p.WEB-DL.x265.tt9999999"
    file_summary = (MediaFileSummary(source_filename, 1000),)
    descriptor = parse_media_name(display_name, total_size=1000, files=file_summary)
    mapping_rules = {
        "source_mappings": [
            {
                "collection_root_name": collection_root,
                "relative_directory": movie_directory,
                "media_title": "Unrelated Movie 1999",
                "confidence_bps": 5000,
                "mapping_status": "SUGGESTED",
            }
        ]
    }

    with factory() as session:
        session.add(
            UnpackTemplate(
                id="template-1",
                name="Local match template",
                current_version=1,
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateVersion(
                id="template-version-1",
                template_id="template-1",
                version=1,
                schema_version=1,
                status="READY",
                expected_seed_count=2,
                seed_count=2,
                site_keys=["mteam"],
                content_digest="a" * 64,
                source_provenance={"source": "test"},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTorrentMetadata(
                id="metadata-1",
                site_key="mteam",
                remote_torrent_id="torrent-template-1",
                display_name=display_name,
                descriptor=_descriptor_payload(descriptor),
                total_size=1000,
                file_summary=[
                    {"basename": item.basename, "length": item.length} for item in file_summary
                ],
                external_ids=[],
                info_hash=None,
                metadata_digest="b" * 64,
                fetched_at=now,
                verification_status="DETAILS_VERIFIED",
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateSeed(
                id="template-seed-1",
                template_version_id="template-version-1",
                metadata_id="metadata-1",
                mapping_rules=mapping_rules,
                sort_order=0,
                created_at=now,
            )
        )
        session.add(
            UnpackTorrentMetadata(
                id="metadata-2",
                site_key="mteam",
                remote_torrent_id="torrent-template-2",
                display_name=display_name,
                descriptor=_descriptor_payload(descriptor),
                total_size=1000,
                file_summary=[
                    {"basename": item.basename, "length": item.length} for item in file_summary
                ],
                external_ids=[],
                info_hash=None,
                metadata_digest="c" * 64,
                fetched_at=now,
                verification_status="DETAILS_VERIFIED",
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateSeed(
                id="template-seed-2",
                template_version_id="template-version-1",
                metadata_id="metadata-2",
                mapping_rules=mapping_rules if duplicate_mapping else {},
                sort_order=1,
                created_at=now,
            )
        )
        definition = session.get(UnpackDefinition, "definition-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert definition is not None and execution is not None
        definition.task_kind = UnpackTaskKind.TEMPLATE.value
        definition.template_id = "template-1"
        definition.template_version = 1
        snapshot = dict(execution.config_snapshot)
        snapshot.update(
            {
                "task_kind": UnpackTaskKind.TEMPLATE.value,
                "template_id": "template-1",
                "template_version": 1,
                "source_config": {"directory_path": f"/mnt/{collection_root}"},
            }
        )
        execution.config_snapshot = snapshot
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.source_snapshot = {
            **dict(item.source_snapshot),
            "path": f"/mnt/{collection_root}/{movie_directory}/{source_filename}",
            "relative_path": f"{movie_directory}/{source_filename}",
        }
        session.commit()

    class LocalTemplateProvider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            raise AssertionError("template matching must not query live site adapters")

        def config_references_for_site_ids(
            self, site_config_ids: tuple[str, ...]
        ) -> tuple[SiteConfigReference, ...]:
            if "site-config-1" not in site_config_ids:
                return ()
            return (SiteConfigReference("site-config-1", 7, "mteam", True),)

    report = asyncio.run(
        UnpackMatchCoordinator(
            factory, LocalTemplateProvider(), pause_after_match=True
        ).match_next_batch("execution-1")
    )

    assert report.processed_count == 1
    assert report.error_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        candidates = session.scalars(
            select(UnpackMatchCandidate).where(UnpackMatchCandidate.item_id == "item-1")
        ).all()
        if duplicate_mapping:
            assert report.matched_auto_count == 0
            assert report.review_count == 1
            assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
            assert item.selected_candidate_id is None
            assert len(candidates) == 2
            assert all(
                candidate.evidence["template_seed_mapping"]["status"] == "AMBIGUOUS"
                for candidate in candidates
            )
        else:
            assert report.matched_auto_count == 1
            assert report.review_count == 0
            assert item.status == UnpackItemStatus.MATCHED_AUTO.value
            assert item.selected_candidate_id is not None
            candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
            assert candidate is not None
            assert candidate.candidate_key == "torrent-template-1"
            assert candidate.site_id == "site-config-1"
            assert candidate.raw_ref["adapter_site_id"] == "mteam"
            assert candidate.raw_ref["site_config_id"] == "site-config-1"
            assert candidate.score_bps < 9900
            assert candidate.evidence["source_mapping"]["matched"] is True
            assert candidate.evidence["source_mapping"]["collection_root_name"] == (
                "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
            )
            assert candidate.evidence["source_mapping"]["relative_directory"] == movie_directory
            assert candidate.evidence["template_seed_mapping"]["status"] == "UNIQUE"
            assert candidate.evidence["template_seed_mapping"]["decision"] == "AUTO_MATCH"
            assert candidate.evidence["template_seed_mapping"]["scope"] == "DIRECTORY"
            assert candidate.verification_status == "NOT_CHECKED"


def test_template_task_reuses_accepted_match_without_site_or_metainfo_check() -> None:
    collection_root = "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
    movie_directory = "Movie.2024.1080p.WEB-DL.x265.tt1234567"
    filename = f"{movie_directory}.mkv"
    relative_path = f"{movie_directory}/{filename}"
    factory = _factory(threshold=9900, filename=filename, size=1000)
    now = utc_now()
    display_name = movie_directory
    descriptor = parse_media_name(display_name, total_size=1000)
    candidate_snapshot = {
        "candidate_key": "torrent-template-1",
        "title": "Movie 2024 accepted seed",
        "size_bytes": 1000,
        "imdb_id": "tt1234567",
        "douban_id": None,
        "seeders": 12,
        "score_bps": 10000,
        "is_exact_match": True,
        "evidence": {
            "evidence_coverage_bps": 7500,
            "hard_conflicts": [],
            "source_mapping": {"mapping_status": "SUGGESTED"},
        },
        "verification_status": "NOT_CHECKED",
        "verification_level": None,
        "verification_error_code": None,
        "metainfo_digest": None,
        "raw_ref": {
            "torrent_id": "torrent-template-1",
            "adapter_site_id": "mteam",
            "site_config_id": "site-config-1",
            "site_config_version": 7,
        },
    }

    with factory() as session:
        session.add(
            UnpackTemplate(
                id="template-1",
                name="Accepted match reuse template",
                current_version=1,
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateVersion(
                id="template-version-1",
                template_id="template-1",
                version=1,
                schema_version=1,
                status="READY",
                expected_seed_count=1,
                seed_count=1,
                site_keys=["mteam"],
                content_digest="a" * 64,
                source_provenance={"source": "test"},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTorrentMetadata(
                id="metadata-1",
                site_key="mteam",
                remote_torrent_id="torrent-template-1",
                display_name=display_name,
                descriptor=_descriptor_payload(descriptor),
                total_size=1000,
                file_summary=[],
                external_ids=[],
                info_hash=None,
                metadata_digest="b" * 64,
                fetched_at=now,
                verification_status="DETAILS_VERIFIED",
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateSeed(
                id="template-seed-1",
                template_version_id="template-version-1",
                metadata_id="metadata-1",
                mapping_rules={},
                sort_order=0,
                created_at=now,
            )
        )
        definition = session.get(UnpackDefinition, "definition-1")
        execution = session.get(UnpackExecution, "execution-1")
        item = session.get(UnpackExecutionItem, "item-1")
        assert definition is not None and execution is not None and item is not None
        definition.task_kind = UnpackTaskKind.TEMPLATE.value
        definition.template_id = "template-1"
        definition.template_version = 1
        execution.config_snapshot = {
            **dict(execution.config_snapshot),
            "task_kind": UnpackTaskKind.TEMPLATE.value,
            "template_id": "template-1",
            "template_version": 1,
            "site_ids": ["site-config-1"],
            "matching": {"auto_match_threshold_bps": 9900, "auto_match_enabled": True},
            "source_config": {"directory_path": f"/mnt/{collection_root}"},
        }
        item.source_snapshot = {
            "path": f"/mnt/{collection_root}/{relative_path}",
            "relative_path": relative_path,
            "device": 11,
            "inode": 22,
            "size": 1000,
            "mtime_ns": "123456789",
            "file_type": "regular",
        }
        session.add(
            Site(
                id="site-config-1",
                name="Accepted reuse MTEAM",
                type="MTEAM",
                base_url="https://synthetic.example",
                credential_kind="API_KEY",
                connection_status="OK",
                enabled=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateMatchAcceptance(
                id="accepted-match-1",
                template_id="template-1",
                site_config_id="site-config-1",
                site_key="mteam",
                collection_root_name=collection_root,
                source_relative_path=relative_path,
                source_device=11,
                source_inode=22,
                source_size_bytes=1000,
                source_mtime_ns="123456789",
                candidate_remote_id="torrent-template-1",
                candidate_title="Movie 2024 accepted seed",
                source_execution_id="previous-execution",
                source_item_id="previous-item",
                source_candidate_id="previous-candidate",
                source_template_version=1,
                accepted_score_bps=10000,
                acceptance_kind="USER_CONFIRMED_PREVIOUS_100_PERCENT",
                candidate_snapshot=candidate_snapshot,
                accepted_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    class NoSiteCalls:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            raise AssertionError("accepted template matches must not query site adapters")

        def config_references_for_site_ids(
            self, site_config_ids: tuple[str, ...]
        ) -> tuple[SiteConfigReference, ...]:
            raise AssertionError("accepted template matches must not request site configuration")

    report = asyncio.run(
        UnpackMatchCoordinator(factory, cast(Any, NoSiteCalls())).match_next_batch("execution-1")
    )
    assert report.processed_count == 1
    assert report.matched_auto_count == 1
    assert report.review_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.status == UnpackItemStatus.MATCHED_AUTO.value
        assert item.selected_candidate_id is not None
        candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert candidate is not None
        assert candidate.candidate_key == "torrent-template-1"
        assert candidate.score_bps == 10000
        assert candidate.verification_status == "NOT_CHECKED"
        assert candidate.metainfo_digest is None
        assert candidate.evidence["evidence_coverage_bps"] == 7500
        assert candidate.evidence["accepted_match_reuse"]["acceptance_id"] == "accepted-match-1"
        assert candidate.evidence["source_mapping"]["mapping_status"] == (
            "USER_ACCEPTED_PREVIOUS_AUTO_MATCH"
        )


def test_template_task_uses_unique_file_identity_when_saved_directory_mapping_is_stale() -> None:
    collection_root = "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
    source_directory = "辩护人.The.Attorney.2013.BluRay.1080p.x265.10bit.DDP.5.1.MNHD-FRDS"
    mapped_directory = "The.Attorney.2013.BluRay.1080p.x265.10bit.DDP.5.1.MNHD-FRDS"
    filename = "The.Attorney.2013.BluRay.1080p.x265.10bit.DDP.5.1.MNHD-FRDS.mkv"
    size = 8_626_699_199
    factory = _factory(threshold=9900, filename=filename, size=size)
    now = utc_now()
    collection_descriptor = parse_media_name(
        "HDTime Movie Collection",
        total_size=size * 2,
        files=(MediaFileSummary(filename, size),),
    )

    with factory() as session:
        session.add(
            UnpackTemplate(
                id="template-1",
                name="Stale directory mapping template",
                current_version=1,
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateVersion(
                id="template-version-1",
                template_id="template-1",
                version=1,
                schema_version=1,
                status="READY",
                expected_seed_count=1,
                seed_count=1,
                site_keys=["mteam"],
                content_digest="a" * 64,
                source_provenance={"source": "test"},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTorrentMetadata(
                id="metadata-1",
                site_key="mteam",
                remote_torrent_id="torrent-template-1",
                display_name="HDTime Movie Collection",
                descriptor=_descriptor_payload(collection_descriptor),
                total_size=size * 2,
                file_summary=[
                    {
                        "basename": filename,
                        "length": size,
                        "path": f"{source_directory}/{filename}",
                    }
                ],
                external_ids=[],
                info_hash="a" * 40,
                metadata_digest="b" * 64,
                fetched_at=now,
                verification_status="DETAILS_VERIFIED",
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateSeed(
                id="template-seed-1",
                template_version_id="template-version-1",
                metadata_id="metadata-1",
                mapping_rules={
                    "source_mappings": [
                        {
                            "collection_root_name": collection_root,
                            "relative_directory": mapped_directory,
                            "media_title": filename.removesuffix(".mkv"),
                            "confidence_bps": 9984,
                            "mapping_status": "SUGGESTED",
                        }
                    ]
                },
                sort_order=0,
                created_at=now,
            )
        )
        definition = session.get(UnpackDefinition, "definition-1")
        execution = session.get(UnpackExecution, "execution-1")
        item = session.get(UnpackExecutionItem, "item-1")
        assert definition is not None and execution is not None and item is not None
        definition.task_kind = UnpackTaskKind.TEMPLATE.value
        definition.template_id = "template-1"
        definition.template_version = 1
        snapshot = dict(execution.config_snapshot)
        snapshot.update(
            {
                "task_kind": UnpackTaskKind.TEMPLATE.value,
                "template_id": "template-1",
                "template_version": 1,
                "source_config": {"directory_path": f"/mnt/{collection_root}"},
            }
        )
        execution.config_snapshot = snapshot
        item.source_snapshot = {
            **dict(item.source_snapshot),
            "path": f"/mnt/{collection_root}/{source_directory}/{filename}",
            "relative_path": f"{source_directory}/{filename}",
        }
        session.commit()

    class LocalTemplateProvider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            raise AssertionError("template matching must use the saved file metadata")

        def config_references_for_site_ids(
            self, site_config_ids: tuple[str, ...]
        ) -> tuple[SiteConfigReference, ...]:
            if "site-config-1" not in site_config_ids:
                return ()
            return (SiteConfigReference("site-config-1", 7, "mteam", True),)

    report = asyncio.run(
        UnpackMatchCoordinator(factory, LocalTemplateProvider()).match_next_batch("execution-1")
    )

    assert report.processed_count == 1
    assert report.matched_auto_count == 1
    assert report.review_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.status == UnpackItemStatus.MATCHED_AUTO.value
        assert item.selected_candidate_id is not None
        candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert candidate is not None and candidate.score_bps == 10_000
        mapping = candidate.evidence["source_mapping"]
        assert mapping["match_mode"] == "UNIQUE_FILE_NAME_AND_SIZE"
        assert mapping["mapping_status"] == "EXACT_FILE_METADATA"
        assert mapping["size_comparison"]["status"] == "MATCHED_FILE"
        assert mapping["size_comparison"]["candidate_file_size_bytes"] == size


def test_unique_template_file_match_fails_closed_when_identity_is_ambiguous() -> None:
    filename = "Movie.2024.mkv"
    file_entry = {"basename": filename, "length": 1000, "path": f"Movie/{filename}"}
    unique = _unique_template_file_match(
        (("seed-1", [file_entry]),),
        source_relative_path=f"Movie/{filename}",
        source_size=1000,
    )
    assert unique == ("seed-1", file_entry)
    windows_style = _unique_template_file_match(
        (("seed-1", [file_entry]),),
        source_relative_path=f"Movie\\{filename}",
        source_size=1000,
    )
    assert windows_style == unique
    assert (
        _unique_template_file_match(
            (("seed-1", [file_entry]), ("seed-2", [file_entry])),
            source_relative_path=f"Movie/{filename}",
            source_size=1000,
        )
        is None
    )
    assert (
        _unique_template_file_match(
            (("seed-1", [file_entry, file_entry]),),
            source_relative_path=f"Movie/{filename}",
            source_size=1000,
        )
        is None
    )


def test_mapped_template_descriptor_preserves_only_single_movie_external_ids() -> None:
    source = replace(
        parse_media_name(
            "Lock Stock and Two Smoking Barrels BluRay 1080p x265 MNHD-FRDS",
            total_size=1000,
        ),
        external_ids=(ExternalMediaId("imdb", "tt0120735"),),
    )
    candidate = replace(
        parse_media_name(
            "Lock Stock and Two Smoking Barrels BluRay 1080p x265 MNHD-FRDS",
            total_size=1200,
        ),
        external_ids=(
            ExternalMediaId("douban", "1293350"),
            ExternalMediaId("imdb", "tt0120735"),
        ),
    )
    mapped = _mapped_template_descriptor(
        "Lock Stock and Two Smoking Barrels BluRay 1080p x265 MNHD-FRDS",
        candidate_descriptor=candidate,
        source_descriptor=source,
        candidate_video_file_count=1,
        total_size=1000,
        files=(MediaFileSummary("Lock.Stock.mkv", 1000),),
    )
    assessment = assess_unpack_candidate(source, mapped)
    assert mapped.external_ids == candidate.external_ids
    assert assessment.evidence["evidence_coverage_bps"] >= 6500
    assert assessment.evidence["exact"]["external_id"] is True
    assert assessment.is_exact_match is True

    collection_mapped = _mapped_template_descriptor(
        "Lock Stock and Two Smoking Barrels BluRay 1080p x265 MNHD-FRDS",
        candidate_descriptor=candidate,
        source_descriptor=source,
        candidate_video_file_count=2,
        total_size=1000,
        files=(MediaFileSummary("Lock.Stock.mkv", 1000),),
    )
    assert collection_mapped.external_ids == ()

    conflicting_candidate = replace(
        candidate,
        external_ids=(ExternalMediaId("imdb", "tt0000001"),),
    )
    conflicting_mapped = _mapped_template_descriptor(
        "Lock Stock and Two Smoking Barrels BluRay 1080p x265 MNHD-FRDS",
        candidate_descriptor=conflicting_candidate,
        source_descriptor=source,
        candidate_video_file_count=1,
        total_size=1000,
        files=(MediaFileSummary("Lock.Stock.mkv", 1000),),
    )
    assert assess_unpack_candidate(source, conflicting_mapped).rejected is True


def test_unmapped_collection_seed_hydration_respects_year_ranges_and_conflicts() -> None:
    source = parse_media_name("Mad Max 2 1981 BluRay 1080p x265", total_size=1_000_000)
    collection = parse_media_name("Mad Max 1979-1985 Collection")
    collection_assessment = assess_unpack_candidate(source, collection)

    assert collection_assessment.rejected is True
    saw_v_source = replace(
        parse_media_name("Saw V 2008 BluRay 1080p", total_size=2_000_000),
        external_ids=(ExternalMediaId("imdb", "tt1132626"),),
    )
    saw_collection = replace(
        parse_media_name("Saw 2004 -2017 Collection"),
        external_ids=(ExternalMediaId("imdb", "tt0387564"),),
    )
    saw_collection_assessment = assess_unpack_candidate(saw_v_source, saw_collection)
    assert saw_collection_assessment.rejected is True
    assert set(saw_collection_assessment.evidence["hard_conflicts"]) == {
        "EXTERNAL_ID_CONFLICT",
        "YEAR_CONFLICT",
    }
    assert (
        _should_hydrate_unmapped_template_candidate(
            source_descriptor=saw_v_source,
            candidate_descriptor=saw_collection,
            candidate_title="Saw 2004 -2017 Collection",
            source_size=saw_v_source.total_size,
            has_metainfo_paths=False,
            has_matching_source_directory_mapping=False,
            assessment=saw_collection_assessment,
        )
        is True
    )
    assert _source_year_matches_title_range(source.year, "Mad Max 1979-1985 Collection")
    assert (
        _should_hydrate_unmapped_template_candidate(
            source_descriptor=source,
            candidate_descriptor=collection,
            candidate_title="Mad Max 1979-1985 Collection",
            source_size=source.total_size,
            has_metainfo_paths=False,
            has_matching_source_directory_mapping=False,
            assessment=collection_assessment,
        )
        is True
    )

    wrong_movie = parse_media_name("Mad Max Fury Road 2015")
    wrong_assessment = assess_unpack_candidate(source, wrong_movie)
    assert (
        _should_hydrate_unmapped_template_candidate(
            source_descriptor=source,
            candidate_descriptor=wrong_movie,
            candidate_title="Mad Max Fury Road 2015",
            source_size=source.total_size,
            has_metainfo_paths=False,
            has_matching_source_directory_mapping=False,
            assessment=wrong_assessment,
        )
        is False
    )

    featurette = parse_media_name("Boundin 2004 Pixar Short", total_size=1_000_000)
    unrelated_movie = parse_media_name("The Incredibles 2004")
    unrelated_assessment = assess_unpack_candidate(featurette, unrelated_movie)
    mapping_rules = {
        "source_mappings": [
            {
                "collection_root_name": "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS",
                "relative_directory": "The.Incredibles.2004",
                "media_title": "The Incredibles 2004",
                "confidence_bps": 10_000,
            }
        ]
    }
    assert _has_matching_seed_directory_mapping(
        mapping_rules,
        collection_root_name="DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS",
        relative_directory="The.Incredibles.2004",
        source_relative_path="The.Incredibles.2004/Boundin.2004.mkv",
    )
    assert not _has_matching_seed_directory_mapping(
        mapping_rules,
        collection_root_name="Different.Collection",
        relative_directory="The.Incredibles.2004",
        source_relative_path="The.Incredibles.2004/Boundin.2004.mkv",
    )
    assert _should_hydrate_unmapped_template_candidate(
        source_descriptor=featurette,
        candidate_descriptor=unrelated_movie,
        candidate_title="The Incredibles 2004",
        source_size=featurette.total_size,
        has_metainfo_paths=False,
        has_matching_source_directory_mapping=True,
        assessment=unrelated_assessment,
    )
    assert (
        _should_hydrate_unmapped_template_candidate(
            source_descriptor=featurette,
            candidate_descriptor=unrelated_movie,
            candidate_title="The Incredibles 2004",
            source_size=featurette.total_size,
            has_metainfo_paths=False,
            has_matching_source_directory_mapping=False,
            assessment=unrelated_assessment,
        )
        is False
    )


def test_mapped_template_seed_hydrates_when_synthetic_title_coverage_is_high() -> None:
    external_id = ExternalMediaId("imdb", "tt1234567")
    source = replace(
        parse_media_name(
            "Movie 2024 1080p BluRay x265 10bit TrueHD 7.1 MNHD-FRDS",
            total_size=10_000_000,
        ),
        external_ids=(external_id,),
    )
    mapped = replace(
        parse_media_name("Movie 2024 1080p BluRay x265 10bit TrueHD 7.1 MNHD-FRDS"),
        external_ids=(external_id,),
    )
    assessment = assess_unpack_candidate(source, mapped)

    assert assessment.rejected is False
    assert int(assessment.evidence["evidence_coverage_bps"]) >= 6_500
    assert (
        _should_hydrate_mapped_template_candidate(
            source_descriptor=source,
            mapped_descriptor=mapped,
            source_size=source.total_size,
            has_metainfo_paths=False,
            has_mapped_file=False,
            confidence_bps=10_000,
            assessment=assessment,
        )
        is True
    )
    assert (
        _should_hydrate_mapped_template_candidate(
            source_descriptor=source,
            mapped_descriptor=mapped,
            source_size=source.total_size,
            has_metainfo_paths=True,
            has_mapped_file=False,
            confidence_bps=10_000,
            assessment=assessment,
        )
        is False
    )
    assert (
        _should_hydrate_mapped_template_candidate(
            source_descriptor=source,
            mapped_descriptor=mapped,
            source_size=source.total_size,
            has_metainfo_paths=False,
            has_mapped_file=True,
            confidence_bps=10_000,
            assessment=assessment,
        )
        is False
    )
    assert (
        _should_hydrate_mapped_template_candidate(
            source_descriptor=source,
            mapped_descriptor=mapped,
            source_size=source.total_size,
            has_metainfo_paths=False,
            has_mapped_file=False,
            confidence_bps=8_999,
            assessment=assessment,
        )
        is False
    )


@pytest.mark.parametrize(
    ("torrent_file_size", "expected_status"),
    [
        (1000, UnpackItemStatus.MATCHED_AUTO.value),
        (800, UnpackItemStatus.MATCHED_AUTO.value),
    ],
)
def test_template_mapping_compares_source_size_with_mapped_torrent_file(
    torrent_file_size: int, expected_status: str
) -> None:
    collection_root = "DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS"
    movie_directory = "Movie.2024.1080p.WEB-DL.x265"
    filename = f"{movie_directory}.mkv"
    factory = _factory(threshold=9900, filename=filename, size=1000)
    now = utc_now()
    minimal_candidate_descriptor = parse_media_name("Movie.2024")
    torrent_payload = _single_file_torrent_for_matching_test(filename, torrent_file_size)

    with factory() as session:
        session.add(
            UnpackTemplate(
                id="template-1",
                name="Size evidence template",
                current_version=1,
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateVersion(
                id="template-version-1",
                template_id="template-1",
                version=1,
                schema_version=1,
                status="READY",
                expected_seed_count=1,
                seed_count=1,
                site_keys=["mteam"],
                content_digest="a" * 64,
                source_provenance={"source": "test"},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTorrentMetadata(
                id="metadata-1",
                site_key="mteam",
                remote_torrent_id="torrent-template-1",
                display_name="Movie.2024",
                descriptor=_descriptor_payload(minimal_candidate_descriptor),
                total_size=None,
                file_summary=[],
                external_ids=[],
                info_hash=None,
                metadata_digest="b" * 64,
                fetched_at=now,
                verification_status="DETAILS_VERIFIED",
                source_provenance={"source": "test"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            UnpackTemplateSeed(
                id="template-seed-1",
                template_version_id="template-version-1",
                metadata_id="metadata-1",
                mapping_rules={
                    "source_mappings": [
                        {
                            "collection_root_name": collection_root,
                            "relative_directory": movie_directory,
                            "media_title": movie_directory,
                            "confidence_bps": 10000,
                            "mapping_status": "SUGGESTED",
                        }
                    ]
                },
                sort_order=0,
                created_at=now,
            )
        )
        definition = session.get(UnpackDefinition, "definition-1")
        execution = session.get(UnpackExecution, "execution-1")
        item = session.get(UnpackExecutionItem, "item-1")
        assert definition is not None and execution is not None and item is not None
        definition.task_kind = UnpackTaskKind.TEMPLATE.value
        definition.template_id = "template-1"
        definition.template_version = 1
        snapshot = dict(execution.config_snapshot)
        snapshot.update(
            {
                "task_kind": UnpackTaskKind.TEMPLATE.value,
                "template_id": "template-1",
                "template_version": 1,
                "source_config": {"directory_path": f"/mnt/{collection_root}"},
            }
        )
        execution.config_snapshot = snapshot
        item.source_snapshot = {
            **dict(item.source_snapshot),
            "path": f"/mnt/{collection_root}/{movie_directory}/{filename}",
            "relative_path": f"{movie_directory}/{filename}",
        }
        session.commit()

    class TorrentOnlyAdapter:
        def __init__(self) -> None:
            self.fetch_calls: list[str] = []

        async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
            self.fetch_calls.append(torrent_id)
            return TorrentPayload(
                site_id="mteam",
                torrent_id=torrent_id,
                content=torrent_payload,
            )

    adapter = TorrentOnlyAdapter()

    class HydratingTemplateProvider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            return ()

        def config_references_for_site_ids(
            self, site_config_ids: tuple[str, ...]
        ) -> tuple[SiteConfigReference, ...]:
            if "site-config-1" not in site_config_ids:
                return ()
            return (SiteConfigReference("site-config-1", 7, "mteam", True),)

        def adapter_for_config(self, site_config_id: str) -> EnabledSiteAdapter:
            assert site_config_id == "site-config-1"
            return EnabledSiteAdapter(
                config_id="site-config-1",
                config_version=7,
                site_id="mteam",
                adapter=cast(SiteAdapter, adapter),
                request_timeout_seconds=10,
                search_interval_seconds=0,
            )

    report = asyncio.run(
        UnpackMatchCoordinator(
            factory, HydratingTemplateProvider(), pause_after_match=True
        ).match_next_batch("execution-1")
    )

    assert report.processed_count == 1
    assert adapter.fetch_calls == ["torrent-template-1"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        metadata = session.get(UnpackTorrentMetadata, "metadata-1")
        assert item is not None and metadata is not None
        assert item.status == expected_status
        assert metadata.total_size == torrent_file_size
        assert metadata.file_summary == [
            {"basename": filename, "length": torrent_file_size, "path": filename}
        ]
        candidates = session.scalars(
            select(UnpackMatchCandidate).where(UnpackMatchCandidate.item_id == "item-1")
        ).all()
        assert len(candidates) == 1
        evidence = candidates[0].evidence
        comparison = evidence["source_mapping"]["size_comparison"]
        assert comparison["status"] == "MATCHED_FILE"
        assert comparison["source_size_bytes"] == 1000
        assert comparison["candidate_file_size_bytes"] == torrent_file_size
        size_dimension = next(
            dimension
            for dimension in evidence["dimensions"]
            if dimension["dimension"] == "TOTAL_SIZE"
        )
        if torrent_file_size == 1000:
            assert size_dimension["earned"] == size_dimension["maximum"]
            assert candidates[0].score_bps == 10000
        else:
            assert size_dimension["earned"] < size_dimension["maximum"]
            assert candidates[0].score_bps < 10000
        assert report.matched_auto_count == 1
        assert report.review_count == 0
        assert candidates[0].evidence["template_seed_mapping"]["status"] == "UNIQUE"


def test_match_threshold_can_auto_select_non_exact_candidate() -> None:
    factory = _factory(threshold=7000)
    adapter = _FakeSiteAdapter((_candidate("Movie.Extended.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1", limit=10))

    assert report.processed_count == 1
    assert report.matched_auto_count == 1
    assert report.execution_status is UnpackExecutionStatus.CONTENT_VERIFYING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCHED_AUTO.value
        assert item.selected_candidate_id is not None
        assert item.candidate_generation == 1
        candidate = session.scalar(select(UnpackMatchCandidate))
        assert candidate is not None
        assert candidate.is_exact_match is False
        assert candidate.score_bps >= 7000


def test_auto_match_disabled_keeps_candidate_for_manual_review() -> None:
    factory = _factory(threshold=7000, auto_match_enabled=False)
    adapter = _FakeSiteAdapter((_candidate("Movie.Extended.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1", limit=10))

    assert report.matched_auto_count == 0
    assert report.review_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        assert item.selected_candidate_id is None


def test_test_pause_keeps_successful_match_in_matching_stage() -> None:
    factory = _factory(threshold=7000)
    adapter = _FakeSiteAdapter((_candidate("Movie.Extended.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(
        factory,
        _FakeSiteProvider(adapter),
        pause_after_match=True,
    )

    report = asyncio.run(service.match_next_batch("execution-1", limit=10))

    assert report.matched_auto_count == 1
    assert report.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCHED_AUTO.value


def test_high_similarity_with_insufficient_evidence_stays_in_review() -> None:
    factory = _factory(threshold=7000, filename="Movie.mkv", size=1000)
    adapter = _FakeSiteAdapter((_candidate("Movie", imdb_id=""),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.review_count == 1
    assert report.matched_auto_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        candidate = session.scalar(select(UnpackMatchCandidate))
        assert candidate is not None
        assert candidate.score_bps == 10_000
        assert candidate.evidence["evidence_coverage_bps"] == 4_000


def test_high_threshold_sends_candidate_to_review_without_persisting_approval() -> None:
    factory = _factory(threshold=10_000)
    adapter = _FakeSiteAdapter((_candidate("Movie.Extended.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.review_count == 1
    assert report.execution_status is UnpackExecutionStatus.REVIEW_REQUIRED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        assert item.selected_candidate_id is None
        assert session.scalar(select(UnpackMatchCandidate)) is not None


def test_empty_first_search_page_is_retried_once_before_declaring_no_match() -> None:
    factory = _factory(threshold=10_000)

    class EmptyThenCandidate(_FakeSiteAdapter):
        def __init__(self) -> None:
            super().__init__((_candidate("Movie.2024.1080p.WEB-DL.x265"),))
            self._empty_once = True

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            if self._empty_once:
                self._empty_once = False
                return SearchPage("fake-site", query.page, (), False)
            return SearchPage("fake-site", query.page, self._candidates, False)

    adapter = EmptyThenCandidate()
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory, _FakeSiteProvider(adapter), max_queries_per_site=1
        ).match_next_batch("execution-1")
    )

    assert len(adapter.queries) == 2
    assert adapter.queries[0] == adapter.queries[1]
    assert report.no_match_count == 0
    assert report.review_count + report.matched_auto_count == 1


def test_empty_year_specific_query_gets_one_fallback_retry_after_other_queries() -> None:
    factory = _factory(
        threshold=10_000,
        filename="Rocky.II.1979.1080p.BluRay.x265.10bit.4Audio.MNHD-FRDS.mkv",
    )

    class EmptyUntilNumericYearFallback(_FakeSiteAdapter):
        def __init__(self) -> None:
            super().__init__((_candidate("Rocky.II.1979.1080p.BluRay.x265"),))
            self._counts: dict[str, int] = {}

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            key = query.query_text
            self._counts[key] = self._counts.get(key, 0) + 1
            if key == "rocky 2 1979" and self._counts[key] == 2:
                return SearchPage("fake-site", query.page, self._candidates, False)
            return SearchPage("fake-site", query.page, (), False)

    adapter = EmptyUntilNumericYearFallback()
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory, _FakeSiteProvider(adapter), max_queries_per_site=4
        ).match_next_batch("execution-1")
    )

    assert [query.query_text for query in adapter.queries].count("rocky 2 1979") == 2
    assert report.no_match_count == 0
    assert report.review_count + report.matched_auto_count == 1


def test_rate_limited_site_uses_broad_title_search_instead_of_false_no_match() -> None:
    factory = _factory(
        threshold=10_000,
        filename="Movie.2024.1080p.WEB-DL.x265.mkv",
    )

    class RateLimitedSite(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            # A real site can return no results for a long "title year" query
            # while succeeding for the bare title.
            return SearchPage(
                site_id="fake-site",
                page=1,
                items=(_candidate("Movie.2024.1080p.WEB-DL.x265"),)
                if len(query.keywords) == 1
                else (),
                has_more=False,
            )

    adapter = RateLimitedSite()
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter)).match_next_batch("execution-1")
    )
    assert adapter.queries
    assert len(adapter.queries[0].keywords) == 1
    assert report.review_count + report.matched_auto_count == 1
    assert report.no_match_count == 0


@pytest.mark.parametrize("english_size", (1600, 1000))
def test_rate_limited_bilingual_fallback_checks_size_before_stopping(english_size: int) -> None:
    filename = "Hail.the.Judge.1994.1080p.BluRay.mkv"
    factory = _factory(threshold=0, filename=filename, size=1000)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = f"/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/{filename}"
        item.source_snapshot = snapshot
        session.commit()

    class BilingualSearch(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            items: tuple[CandidateMeta, ...]
            if query.query_text == "hail the judge":
                items = (
                    _candidate(
                        "Hail.the.Judge.1994.1080p.BluRay", torrent_id="english", size=english_size
                    ),
                )
            elif query.query_text == "九品芝麻官":
                items = (
                    _candidate("九品芝麻官.1994.1080p.BluRay", torrent_id="chinese", size=1000),
                )
            else:
                items = ()
            return SearchPage("fake-site", 1, items, False)

    adapter = BilingualSearch()
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory, _FakeSiteProvider(adapter), max_queries_per_site=2
        ).match_next_batch("execution-1")
    )
    assert report.matched_auto_count == 1
    expected_queries = (
        ["hail the judge", "九品芝麻官"] if english_size == 1600 else ["hail the judge"]
    )
    assert [query.query_text for query in adapter.queries] == expected_queries
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.selected_candidate_id
        selected = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert selected is not None
        assert selected.candidate_key == ("chinese" if english_size == 1600 else "english")


def test_search_paginates_within_configured_bound_and_finds_later_page_candidate() -> None:
    factory = _factory(threshold=10_000)

    class PaginatedSite(_FakeSiteAdapter):
        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            if query.page == 1:
                return SearchPage("fake-site", query.page, (), True, 100)
            return SearchPage(
                "fake-site",
                query.page,
                (_candidate("Movie.2024.1080p.WEB-DL.x265"),),
                False,
                100,
            )

    adapter = PaginatedSite()
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory,
            _FakeSiteProvider(adapter),
            max_queries_per_site=1,
            max_pages_per_query=2,
        ).match_next_batch("execution-1")
    )

    assert [query.page for query in adapter.queries] == [1, 2]
    assert report.review_count == 1
    assert report.no_match_count == 0


def test_extra_name_exclusion_skips_bonus_feature_without_site_search() -> None:
    factory = _factory(threshold=10_000, filename="SP.BURN.E.2008.1080p.BluRay.mkv")
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        item = session.get(UnpackExecutionItem, "item-1")
        assert execution is not None and item is not None
        snapshot = dict(execution.config_snapshot)
        snapshot["source_config"] = {"directory_path": "/data"}
        snapshot["file_filter"] = {"exclude_names": ["sample", "trailer"]}
        execution.config_snapshot = snapshot
        session.commit()

    adapter = _FakeSiteAdapter()
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory,
            _FakeSiteProvider(adapter),
            extra_exclude_names=("sp.",),
        ).match_next_batch("execution-1")
    )

    assert adapter.queries == []
    assert report.processed_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.CANCELLED.value
        assert item.last_error_code == "UNPACK_MEDIA_EXCLUDED_BY_FILTER"
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "SKIPPED_BY_EXCLUSION"


def test_search_diagnostics_explain_each_title_stage_without_leaking_queries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    filename = "Hail.the.Judge.1994.1080p.BluRay.mkv"
    factory = _factory(threshold=10_000, filename=filename, size=7_600_000_000)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = "/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/" + filename
        item.source_snapshot = snapshot
        session.commit()

    class RousiStyleSearch(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            candidates: tuple[CandidateMeta, ...]
            if query.query_text == "hail the judge":
                candidates = (_candidate("The.Little.Death.2014.1080p.BluRay", torrent_id="99"),)
            elif query.query_text == "九品芝麻官":
                candidates = (
                    _candidate("九品芝麻官.1994.1080p.BluRay", torrent_id="101"),
                    _candidate("九品芝麻官.2014.1080p.BluRay", torrent_id="102"),
                    _candidate("Unrelated.Movie.1994.1080p", torrent_id="103"),
                    _candidate("九品芝麻官.1994.720p.BluRay", torrent_id="104"),
                )
            else:
                candidates = ()
            return SearchPage("fake-site", 1, candidates, False, len(candidates))

    adapter = RousiStyleSearch()
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter), max_queries_per_site=2)
    with caplog.at_level(logging.INFO, logger="packbreaker.unpack.search_diagnostics"):
        report = asyncio.run(service.match_next_batch("execution-1"))
    assert [q.query_text for q in adapter.queries] == ["hail the judge", "九品芝麻官"]
    assert report.review_count == 1
    records = [
        record
        for record in caplog.records
        if record.name == "packbreaker.unpack.search_diagnostics"
    ]
    assert [record.__dict__["fields"]["query_stage"] for record in records] == [
        "ENGLISH_TITLE",
        "CHINESE_TITLE",
    ]
    assert records[0].__dict__["fields"]["returned_count"] == 1
    assert records[1].__dict__["fields"]["returned_count"] == 4
    assert records[1].__dict__["fields"]["hard_conflict_count"] >= 1
    assert records[1].__dict__["fields"]["selectable_count"] >= 1
    assert records[1].__dict__["fields"]["title_filtered_count"] >= 1
    assert "九品芝麻官" not in caplog.text
    assert "hail the judge" not in caplog.text
    assert "SECRET_PRIVATE_KEY" not in caplog.text


def test_real_rousi_adapter_mock_transport_keeps_chinese_search_candidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No external traffic: exercise real query serialization and matcher together."""
    filename = "Hail.the.Judge.1994.1080p.BluRay.mkv"
    factory = _factory(threshold=10_000, filename=filename, size=7_600_000_000)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = "/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/" + filename
        item.source_snapshot = snapshot
        session.commit()

    searched: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/torrents"
        assert request.url.params["offset"] == "0"
        assert request.url.params["limit"] == "50"
        assert request.headers.get("api-token") is None
        assert request.headers.get("cookie") is None
        query = request.url.params["query"]
        searched.append(query)
        titles = (
            ["The Little Death 2014 1080p BluRay x265"]
            if query == "hail the judge"
            else [
                "九品芝麻官 1994 1080p BluRay",
                "九品芝麻官 2014 1080p BluRay",
                "Unrelated Movie 1994 WEB-DL",
                "九品芝麻官 1994 720p BluRay",
            ]
        )
        return httpx2.Response(
            200,
            json={
                "limit": 50,
                "offset": 0,
                "total": len(titles),
                "items": [
                    {
                        "id": i + (100 if query == "hail the judge" else 200),
                        "name": title,
                        "size_bytes": 7_600_000_000,
                    }
                    for i, title in enumerate(titles)
                ],
            },
        )

    adapter = RousiProCandidateAdapter(
        "synthetic-api-key-private", transport=httpx2.MockTransport(handler)
    )

    class Provider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            return (
                EnabledSiteAdapter(
                    config_id="site-config-1",
                    config_version=7,
                    site_id="rousi_pro",
                    adapter=cast(SiteAdapter, adapter),
                ),
            )

    with caplog.at_level(logging.INFO, logger="packbreaker.unpack.search_diagnostics"):
        result = asyncio.run(
            UnpackMatchCoordinator(factory, Provider(), max_queries_per_site=2).match_next_batch(
                "execution-1"
            )
        )
    assert searched == ["hail the judge", "九品芝麻官"]
    assert result.review_count == 1
    diagnostic = [
        r.__dict__["fields"]
        for r in caplog.records
        if r.name == "packbreaker.unpack.search_diagnostics"
    ]
    assert [item["returned_count"] for item in diagnostic] == [1, 4]
    assert diagnostic[1]["query_stage"] == "CHINESE_TITLE"
    assert diagnostic[1]["selectable_count"] >= 1
    assert "synthetic-api-key-private" not in caplog.text
    assert "九品芝麻官" not in caplog.text


def test_all_unmatched_movies_result_in_failed_not_partially_completed() -> None:
    factory = _factory(threshold=10_000)
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(_FakeSiteAdapter())).match_next_batch(
            "execution-1"
        )
    )
    assert report.no_match_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_message is not None
        assert "未找到符合影片名称" in item.last_error_message


def test_hard_conflict_candidate_is_not_auto_selected() -> None:
    factory = _factory(threshold=0)
    adapter = _FakeSiteAdapter(
        (
            _candidate(
                "Movie.2023.1080p.WEB-DL.x265",
                imdb_id="tt7654321",
            ),
        )
    )
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.no_match_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.NO_MATCH.value
        assert item.last_error_message is not None
        assert "年份或身份冲突" in item.last_error_message
        candidate = session.scalar(select(UnpackMatchCandidate))
        assert candidate is not None
        assert candidate.evidence["hard_conflicts"]


def test_retryable_site_failure_becomes_match_timeout() -> None:
    factory = _factory(threshold=10_000)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_TIMEOUT",
            "synthetic timeout",
            retryable=True,
        )
    )
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.timeout_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_TIMEOUT.value
        assert item.last_error_code == "UNPACK_MATCH_TIMEOUT"


def test_test_pause_keeps_retryable_timeout_in_matching_stage_without_retry() -> None:
    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=3)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_TIMEOUT",
            "synthetic timeout",
            retryable=True,
        )
    )
    service = UnpackMatchCoordinator(
        factory,
        _FakeSiteProvider(adapter),
        pause_after_match=True,
    )

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.timeout_count == 1
    assert report.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_TIMEOUT.value
        assert item.retry_count == 0


def test_nonretryable_site_failure_does_not_consume_automatic_retry_budget() -> None:
    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=3)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_AUTH_FAILED", "synthetic private reason", retryable=False)
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert report.processed_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SITE_AUTH_FAILED"
        assert item.retry_count == 0
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["search_failures"][0]["retryable"] is False
    # A terminal credential error must not trigger new query variations or worker loops.
    assert len(adapter.queries) == 1
    assert asyncio.run(coordinator.match_next_batch("execution-1")).processed_count == 0


def test_retryable_site_unavailable_exhaustion_is_match_error_not_timeout() -> None:
    from datetime import timedelta

    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=1)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_UNAVAILABLE", "temporary outage", retryable=True)
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))
    first = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert first.execution_status is UnpackExecutionStatus.MATCHING
    assert len(adapter.queries) == 1  # no keyword fanout after transient failure
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.retry_count == 1
        state = dict(item.auxiliary_state or {})
        state["auto_retry_not_before"] = (utc_now() - timedelta(seconds=5)).isoformat()
        item.auxiliary_state = state
        session.commit()

    final = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert final.processed_count == 1
    assert final.execution_status is UnpackExecutionStatus.FAILED
    assert final.error_count == 1
    assert final.timeout_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SITE_UNAVAILABLE"
        assert item.retry_count == 1


def test_site_retry_after_extends_automatic_match_backoff() -> None:
    from datetime import datetime, timedelta

    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=2)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_RATE_LIMITED",
            "synthetic throttle",
            retryable=True,
            retry_after_seconds=120.0,
        )
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    started = utc_now()
    report = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert report.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.retry_count == 1
        state = item.auxiliary_state or {}
        due = datetime.fromisoformat(state["auto_retry_not_before"])
        assert due >= started + timedelta(seconds=120)
    assert len(adapter.queries) == 1
    assert asyncio.run(coordinator.match_next_batch("execution-1")).processed_count == 0


@pytest.mark.parametrize("wait", (float("inf"), float("nan"), -10.0, 86401.0))
def test_invalid_or_unbounded_site_retry_after_fails_closed(wait: float) -> None:
    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=3)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_RATE_LIMITED",
            "synthetic invalid cooldown",
            retryable=True,
            retry_after_seconds=wait,
        )
    )
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter)).match_next_batch("execution-1")
    )
    assert report.execution_status is UnpackExecutionStatus.FAILED
    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SITE_RATE_LIMITED"
        assert item.retry_count == 0


def test_match_timeout_automatically_retries_with_backoff_then_needs_manual_action() -> None:
    from datetime import timedelta

    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=2)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_TIMEOUT", "synthetic timeout", retryable=True)
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    first = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert first.processed_count == 1
    assert first.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == "MATCH_PENDING"
        assert item.retry_count == 1
        assert "auto_retry_not_before" in (item.auxiliary_state or {})

    # Backoff prevents spinning all retries inside one worker tick.
    assert asyncio.run(coordinator.match_next_batch("execution-1")).processed_count == 0

    for attempt in (2, 3):
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            state = dict(item.auxiliary_state or {})
            state["auto_retry_not_before"] = (utc_now() - timedelta(seconds=5)).isoformat()
            item.auxiliary_state = state
            session.commit()
        report = asyncio.run(coordinator.match_next_batch("execution-1"))
        assert report.processed_count == 1
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            if attempt == 2:
                assert item.status == "MATCH_PENDING"
                assert item.retry_count == 2
                assert report.execution_status is UnpackExecutionStatus.MATCHING
            else:
                assert item.status == "MATCH_TIMEOUT"
                assert item.retry_count == 2
                assert report.execution_status is UnpackExecutionStatus.FAILED


def test_no_match_never_consumes_automatic_retry_budget() -> None:
    factory = _factory(
        threshold=10_000,
        filename="Movie.2024.1080p.WEB-DL.x265.mkv",
        retry_enabled=True,
        max_retries=5,
    )
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(_FakeSiteAdapter())).match_next_batch(
            "execution-1"
        )
    )
    assert report.no_match_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.status == "NO_MATCH"
        assert item.retry_count == 0


def test_test_pause_keeps_no_match_in_matching_stage() -> None:
    factory = _factory(
        threshold=10_000,
        filename="Movie.2024.1080p.WEB-DL.x265.mkv",
        retry_enabled=True,
        max_retries=5,
    )
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory,
            _FakeSiteProvider(_FakeSiteAdapter()),
            pause_after_match=True,
        ).match_next_batch("execution-1")
    )

    assert report.no_match_count == 1
    assert report.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.NO_MATCH.value
        assert item.retry_count == 0


def test_rousi_suspicious_identical_unfiltered_pages_are_not_no_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from dataclasses import replace

    from backend.app.application.sites import EnabledSiteAdapter

    factory = _factory(threshold=10_000, filename="Hail.the.Judge.1994.1080p.BluRay.mkv")
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = (
            "/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/Hail.the.Judge.1994.1080p.BluRay.mkv"
        )
        item.source_snapshot = snapshot
        session.commit()

    candidates = tuple(
        replace(
            _candidate(f"Unrelated.Film.{2000 + i % 18}", torrent_id=str(i + 10)),
            site_id="rousi_pro",
        )
        for i in range(100)
    )

    class UnfilteredRousi(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            return SearchPage("rousi_pro", query.page, candidates, False, 8995)

    adapter = UnfilteredRousi()

    class RousiProvider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            return (
                EnabledSiteAdapter("site-config-1", 1, "rousi_pro", cast(SiteAdapter, adapter)),
            )

    with caplog.at_level(logging.INFO, logger="packbreaker.unpack.search_diagnostics"):
        report = asyncio.run(
            UnpackMatchCoordinator(
                factory, RousiProvider(), max_queries_per_site=4
            ).match_next_batch("execution-1")
        )
    assert [q.query_text for q in adapter.queries] == ["hail the judge", "九品芝麻官"]
    assert report.error_count == 1
    assert report.no_match_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == "MATCH_ERROR"
        assert item.last_error_code == "SITE_SEARCH_FILTER_IGNORED_SUSPECTED"
    assert any(
        record.__dict__.get("fields", {}).get("outcome") == "QUERY_FILTER_IGNORED_SUSPECTED"
        for record in caplog.records
    )


def test_per_request_timeout_does_not_include_successive_search_and_pacing() -> None:
    """Two healthy slower requests plus a deliberate interval must not trip
    a single 40-ms global budget (the previous behavior).
    """

    class TwoQueriesAdapter(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(
                supports_imdb_id=True,
                supports_douban_id=True,
                min_request_interval_seconds=0.03,
            )

        async def search(self, query: SearchQuery) -> SearchPage:
            await asyncio.sleep(0.03)
            return await super().search(query)

    factory = _factory(threshold=10_000)
    adapter = TwoQueriesAdapter((_candidate("Completely.Unrelated.2010.720p.WEB-DL"),))
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory,
            _FakeSiteProvider(adapter),
            max_queries_per_site=2,
            max_pages_per_query=1,
            site_timeout_seconds=0.04,
        ).match_next_batch("execution-1")
    )
    assert report.processed_count == 1
    assert len(adapter.queries) == 2
    assert report.timeout_count == 0


def test_per_request_deadline_respects_site_specific_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured 60-sec HTTP + 60-sec pacing overrides 30-sec match default."""
    from backend.app.application import unpack_matching

    observed: list[float] = []

    async def record_call(operation: object, *, timeout_seconds: float) -> object:
        observed.append(timeout_seconds)
        return await cast(Any, operation)

    monkeypatch.setattr(unpack_matching, "_await_site_request", record_call)
    adapter = _FakeSiteAdapter((_candidate("Movie.2024.1080p.WEB-DL.x265"),))

    class ConfiguredProvider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            return (
                EnabledSiteAdapter(
                    "site-config-1",
                    7,
                    "fake-site",
                    cast(SiteAdapter, adapter),
                    request_timeout_seconds=60,
                    search_interval_seconds=60,
                ),
            )

    report = asyncio.run(
        UnpackMatchCoordinator(
            _factory(threshold=10_000),
            ConfiguredProvider(),
            site_timeout_seconds=30,
            max_queries_per_site=1,
            max_pages_per_query=1,
        ).match_next_batch("execution-1")
    )
    assert report.processed_count == 1
    assert observed and all(seconds == 121.0 for seconds in observed)


def test_risk_threshold_parks_current_item_for_manual_site_reenable() -> None:
    factory = _factory(threshold=10_000, retry_enabled=True)
    _add_pending_item(factory, item_id="item-2")
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_RATE_LIMITED", "synthetic rate limit", retryable=True)
    )

    class RiskProvider(_FakeSiteProvider):
        def record_unpack_risk_event(
            self, *, execution_id: str, item_id: str, site_config_id: str, error_code: str
        ) -> SiteRiskControlOutcome:
            assert (execution_id, item_id, site_config_id, error_code) == (
                "execution-1",
                "item-1",
                "site-config-1",
                "SITE_RATE_LIMITED",
            )
            return SiteRiskControlOutcome(
                risk_count=3,
                risk_limit=3,
                threshold_reached=True,
                retry_interval_seconds=120,
                risk_disabled_at=utc_now(),
            )

    report = asyncio.run(
        UnpackMatchCoordinator(
            factory,
            RiskProvider(adapter),
            max_queries_per_site=4,
            max_pages_per_query=1,
            site_timeout_seconds=1,
        ).match_next_batch("execution-1")
    )

    assert report.processed_count == 1
    assert len(adapter.queries) == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.last_error_code == "SITE_RISK_CONTROL_LIMIT_REACHED"
        state = item.auxiliary_state or {}
        assert state["risk_control_wait"]["requires_manual_reenable"] is True
        assert state["risk_control_wait"]["risk_count"] == 3
        not_before = datetime.fromisoformat(state["auto_retry_not_before"])
        assert not_before > utc_now() + timedelta(seconds=100)
        other = session.get(UnpackExecutionItem, "item-2")
        assert other is not None
        assert other.status == UnpackItemStatus.MATCH_PENDING.value
        assert (other.auxiliary_state or {})["risk_control_wait"][
            "requires_manual_reenable"
        ] is True


def test_first_risk_event_defers_execution_by_site_configured_interval() -> None:
    factory = _factory(threshold=10_000, retry_enabled=True)
    _add_pending_item(factory, item_id="item-2")
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_RATE_LIMITED", "synthetic rate limit", retryable=True)
    )

    class RiskProvider(_FakeSiteProvider):
        def record_unpack_risk_event(
            self, *, execution_id: str, item_id: str, site_config_id: str, error_code: str
        ) -> SiteRiskControlOutcome:
            return SiteRiskControlOutcome(
                risk_count=1,
                risk_limit=3,
                threshold_reached=False,
                retry_interval_seconds=120,
                risk_disabled_at=None,
            )

    report = asyncio.run(
        UnpackMatchCoordinator(
            factory,
            RiskProvider(adapter),
            max_queries_per_site=1,
            max_pages_per_query=1,
            site_timeout_seconds=1,
        ).match_next_batch("execution-1")
    )
    assert report.processed_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.last_error_code == "SITE_RISK_CONTROL_COOLDOWN"
        state = item.auxiliary_state or {}
        assert state["risk_control_wait"]["requires_manual_reenable"] is False
        assert datetime.fromisoformat(state["auto_retry_not_before"]) > utc_now() + timedelta(
            seconds=100
        )
        other = session.get(UnpackExecutionItem, "item-2")
        assert other is not None
        other_state = other.auxiliary_state or {}
        assert other.status == UnpackItemStatus.MATCH_PENDING.value
        assert other_state["risk_control_wait"]["requires_manual_reenable"] is False
        assert datetime.fromisoformat(other_state["auto_retry_not_before"]) > utc_now()


def test_auto_disabled_site_defers_due_items_without_sending_more_requests() -> None:
    factory = _factory(threshold=10_000)
    now = utc_now()
    with factory() as session:
        session.add(
            Site(
                id="site-config-1",
                name="自动风控停用站点",
                type="HDTIME",
                base_url="https://hdtime.org",
                credential_kind="COOKIE",
                secret_id=None,
                enabled=False,
                risk_control_limit=3,
                risk_retry_interval_seconds=120,
                risk_disabled_at=now,
            )
        )
        session.commit()

    adapter = _FakeSiteAdapter((_candidate("Movie.2024.1080p.WEB-DL.x265"),))
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter)).match_next_batch("execution-1")
    )
    assert report.processed_count == 0
    assert not adapter.queries
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        state = item.auxiliary_state or {}
        assert state["risk_control_wait"]["requires_manual_reenable"] is True
        assert datetime.fromisoformat(state["auto_retry_not_before"]) > utc_now()


def test_selected_site_not_enabled_becomes_match_error() -> None:
    factory = _factory(threshold=10_000)
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(None))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_MATCH_SITE_UNAVAILABLE"
        assert "启用" in (item.last_error_message or "")


def test_missing_selected_site_blocks_all_other_site_searches() -> None:
    """An already-running task must not silently shrink its configured scan sites."""
    factory = _factory(threshold=0)
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.config_snapshot = {
            **execution.config_snapshot,
            "site_ids": ["site-config-1", "site-config-disabled"],
        }
        session.commit()

    adapter = _FakeSiteAdapter((_candidate("Movie.2024.1080p.WEB-DL.x265"),))
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))
    report = asyncio.run(coordinator.match_next_batch("execution-1"))

    assert report.processed_count == 1
    assert report.error_count == 1
    assert not adapter.queries  # missing scan site must block every outbound request
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_MATCH_SITE_UNAVAILABLE"
        assert session.scalar(select(UnpackMatchCandidate)) is None


def test_empty_scan_sites_snapshot_is_error_not_no_match() -> None:
    """A malformed historical execution cannot report successful site scanning."""
    factory = _factory(threshold=0)
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.config_snapshot = {**execution.config_snapshot, "site_ids": []}
        session.commit()

    adapter = _FakeSiteAdapter((_candidate("Movie.2024.1080p.WEB-DL.x265"),))
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter)).match_next_batch("execution-1")
    )

    assert report.error_count == 1
    assert not adapter.queries
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_MATCH_SITE_UNAVAILABLE"


def test_custom_media_suffix_is_not_rejected_by_legacy_task_unit_extensions() -> None:
    factory = _factory(
        threshold=0,
        filename="Movie.2024.1080p.WEB-DL.x265.tt1234567.customvideo",
    )
    adapter = _FakeSiteAdapter((_candidate("Movie.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.matched_auto_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.media_identity["raw_name"].startswith("Movie.2024")


def test_unparseable_media_identity_becomes_terminal_match_error_for_this_generation() -> None:
    factory = _factory(threshold=10_000, filename="---.mkv")
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(None))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.processed_count == 0
    assert report.error_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_MEDIA_IDENTITY_INVALID"


def test_season_directory_context_recovers_bare_episode_identity() -> None:
    factory = _factory(
        threshold=0,
        filename="Show.2024/Season 01/01.customvideo",
    )
    adapter = _FakeSiteAdapter(
        (
            _candidate(
                "Show.2024.S01E01.1080p.WEB-DL.x265",
                imdb_id="tt1234567",
            ),
        )
    )
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.matched_auto_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCHED_AUTO.value
        assert item.media_identity["title_tokens"] == ["show"]
        assert item.media_identity["year"] == 2024
        assert item.media_identity["episode"] == {
            "kind": "SEASON_EPISODE",
            "season": 1,
            "start": 1,
            "end": 1,
        }
