"""做种健康巡查的判定真值表与服务集成测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.seeding_health import (
    SeedingHealthService,
    SeedingHealthSettings,
)
from backend.app.domain.downloader import PathMappingRule
from backend.app.domain.seeding_health import (
    SeedingIdentitySource,
    SeedingIssueKind,
    SeedingIssueStatus,
    SeedingRunTrigger,
    SeedingWatchPresence,
    SiteProbeOutcome,
    SiteProbeTally,
    hnr_remaining_hours,
    is_site_degraded,
    risk_score,
    severity_for,
    should_clear_issue,
    should_confirm_missing,
    status_on_clear,
    status_on_redetect,
)
from backend.app.domain.site_adapter import SiteSeedingItem, SiteSeedingList
from backend.app.domain.torrent_identity import identity_from_comment, identity_from_payload
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.adapters.downloaders import DownloaderTorrent
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import configure_sqlite_engine
from backend.app.infrastructure.persistence.models import (
    Downloader,
    SeedingHealthIssue,
    SeedingWatch,
)


def _factory(db_path: Path) -> tuple[sessionmaker[Session], object]:
    # 巡查用 asyncio.to_thread 访问数据库，内存库在不同线程会是不同的库，
    # 因此测试必须用文件库才能让多线程共享同一份 schema。
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    # 与生产连接语义一致：没有外键断言，虚拟 watch 的外键 bug 测不出来。
    engine = configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    return factory, engine


def _torrent(
    torrent_hash: str,
    *,
    content_path: str | None = "/downloads/movie.mkv",
    progress: float = 1.0,
    tracker_state: str | None = None,
    comment: str | None = "https://hdtime.org/details.php?id=123456",
    tracker: str | None = "https://tracker.example/announce",
    tracker_urls: tuple[str, ...] = (),
) -> DownloaderTorrent:
    return DownloaderTorrent(
        torrent_hash=torrent_hash,
        name="Dune Part Two 2024 2160p",
        status="uploading",
        progress=progress,
        size_bytes=50_000_000_000,
        category=None,
        tags=(),
        tracker=tracker,
        tracker_urls=tracker_urls,
        save_path="/downloads",
        content_path=content_path,
        seeding_time_seconds=3600,
        tracker_state=tracker_state,
        comment=comment,
    )


def _torrent_bytes(comment: str) -> bytes:
    encoded = comment.encode("utf-8")
    return (
        b"d"
        + b"7:comment"
        + str(len(encoded)).encode("ascii")
        + b":"
        + encoded
        + b"4:infod4:name1:xee"
    )


class _FakeSiteAdapter:
    def __init__(self, *, error: str | None = None, seeding_ids: tuple[str, ...] = ()) -> None:
        self.error = error
        self.calls = 0
        self.seeding_ids = seeding_ids

    async def list_seeding(self, cursor: str | None = None) -> SiteSeedingList:
        return SiteSeedingList(
            supported=True,
            items=tuple(
                SiteSeedingItem(torrent_id=item, title=f"站点种子 {item}")
                for item in self.seeding_ids
            ),
            next_cursor=None,
            total_hint=len(self.seeding_ids),
        )

    async def fetch_details(self, torrent_id: str) -> object:
        self.calls += 1
        if self.error == "missing":
            raise SiteAdapterError("SITE_NOT_FOUND", "not found")
        if self.error == "auth":
            raise SiteAdapterError("SITE_AUTH_FAILED", "cookie expired")
        if self.error == "parse":
            raise SiteAdapterError("SITE_INVALID_RESPONSE", "cannot parse")
        return SimpleNamespace(torrent_id=torrent_id)


class _FakeDownloaderAdapter:
    def __init__(self, site_adapter: _FakeSiteAdapter, *, identity: str = "hdtime") -> None:
        self._site_adapter = site_adapter
        self._identity = identity

    async def export_torrent(self, torrent_hash: str) -> bytes:
        if self._identity == "none":
            raise RuntimeError("export failed")
        comment = "https://hdtime.org/details.php?id=123456"
        return _torrent_bytes(comment)


class _FakeDownloaderService:
    def __init__(
        self,
        torrents: tuple[DownloaderTorrent, ...],
        adapter: _FakeDownloaderAdapter,
        *,
        mappings: list[PathMappingRule] | None = None,
    ) -> None:
        self._torrents = torrents
        self._adapter = adapter
        self._mappings = mappings or []
        self.binding = SimpleNamespace(adapter=adapter)

    def list_downloaders(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                id="dl-1",
                name="qBittorrent",
                enabled=True,
                type=DownloaderKind.QBITTORRENT,
                path_mappings=self._mappings,
            )
        ]

    async def list_all_torrents(self, downloader_id: str) -> tuple[DownloaderTorrent, ...]:
        return self._torrents

    def write_binding(self, downloader_id: str) -> SimpleNamespace:
        return self.binding


class _FakeSiteService:
    def __init__(self, adapter: _FakeSiteAdapter) -> None:
        self._adapter = adapter

    def enabled_adapters(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(site_id="hdtime", adapter=self._adapter),)


def _setup(
    tmp_path: Path,
    *,
    site_error: str | None,
    torrents: tuple[DownloaderTorrent, ...] | None = None,
    mappings: list[PathMappingRule] | None = None,
    db_name: str = "health.db",
) -> tuple[SeedingHealthService, sessionmaker[Session], _FakeSiteAdapter]:
    factory, engine = _factory(tmp_path / db_name)
    with factory() as session:
        session.add(
            Downloader(
                id="dl-1",
                name="qBittorrent",
                type=DownloaderKind.QBITTORRENT.value,
                base_url="http://127.0.0.1:8080",
                path_mappings=[],
                capabilities={},
                connection_status="OK",
                path_mapping_status="OK",
                enabled=True,
                version=1,
                monitor_rules={},
            )
        )
        session.commit()
    site_adapter = _FakeSiteAdapter(error=site_error)
    downloader_adapter = _FakeDownloaderAdapter(site_adapter)
    service = SeedingHealthService(
        factory,
        _FakeDownloaderService(
            # 站点侧探测只针对 tracker 报错的种子，测试默认值必须带该信号。
            torrents if torrents is not None else (_torrent("a" * 40, tracker_state="ERROR"),),
            downloader_adapter,
            mappings=mappings,
        ),
        _FakeSiteService(site_adapter),
        settings=SeedingHealthSettings(
            confirm_threshold=1, detail_ttl_hours=24, site_probe_enabled=True
        ),
    )
    return service, factory, site_adapter


def _issues_of(session: Session, kind: SeedingIssueKind) -> list[SeedingHealthIssue]:
    return list(
        session.scalars(
            select(SeedingHealthIssue).where(SeedingHealthIssue.kind == kind.value)
        ).all()
    )


def _run(service: SeedingHealthService) -> None:
    asyncio.run(service.run_once(SeedingRunTrigger.MANUAL))


def test_domain_truth_table() -> None:
    assert should_confirm_missing(SiteProbeOutcome.MISSING, 1, 2) is False
    assert should_confirm_missing(SiteProbeOutcome.MISSING, 2, 2) is True
    assert should_confirm_missing(SiteProbeOutcome.UNKNOWN, 9, 2) is False
    assert should_confirm_missing(SiteProbeOutcome.FOUND, 9, 2) is False

    assert should_clear_issue(SiteProbeOutcome.FOUND) is True
    assert should_clear_issue(SiteProbeOutcome.UNKNOWN) is False
    assert should_clear_issue(SiteProbeOutcome.MISSING) is False

    assert status_on_redetect(None) is SeedingIssueStatus.OPEN
    assert status_on_redetect(SeedingIssueStatus.RESOLVED) is SeedingIssueStatus.OPEN
    assert status_on_redetect(SeedingIssueStatus.IGNORED) is SeedingIssueStatus.OPEN
    assert status_on_redetect(SeedingIssueStatus.ACKED) is SeedingIssueStatus.ACKED
    assert status_on_clear(SeedingIssueStatus.ACKED) is SeedingIssueStatus.RESOLVED

    assert severity_for(50).value == "MEDIUM"
    assert severity_for(79).value == "MEDIUM"
    assert severity_for(80).value == "HIGH"
    assert risk_score(SeedingIssueKind.FILE_MISSING) == 55


def test_hnr_remaining_never_guesses() -> None:
    assert hnr_remaining_hours(required_hours=None, seeding_seconds=3600) is None
    assert hnr_remaining_hours(required_hours=72, seeding_seconds=None) is None
    assert hnr_remaining_hours(required_hours=72, seeding_seconds=72 * 3600) == 0.0
    assert hnr_remaining_hours(required_hours=72, seeding_seconds=0) == 72.0


def test_site_degradation_requires_sample_size() -> None:
    small = SiteProbeTally(site_key="hdtime", probed=5, missing=5, unknown=0)
    assert is_site_degraded(small) is False
    big = SiteProbeTally(site_key="hdtime", probed=20, missing=12, unknown=0)
    assert is_site_degraded(big) is True
    healthy = SiteProbeTally(site_key="hdtime", probed=20, missing=2, unknown=0)
    assert is_site_degraded(healthy) is False


def test_identity_from_comment_and_payload() -> None:
    identity = identity_from_comment("https://hdtime.org/details.php?id=998877")
    assert identity is not None
    assert identity.site_key == "hdtime"
    assert identity.torrent_id == "998877"
    assert identity.source.value == "COMMENT"

    mteam = identity_from_comment("https://kp.m-team.cc/detail/123456")
    assert mteam is not None
    assert mteam.site_key == "mteam"
    assert mteam.torrent_id == "123456"

    assert identity_from_comment("no link here") is None
    assert identity_from_payload(_torrent_bytes("https://hdtime.org/details.php?id=1")) is not None


def test_multi_tracker_uses_first_recognized_announce(tmp_path: Path) -> None:
    """首个 tracker 不是 PT 站时，要接着往后找，而不是直接回退 comment。"""

    torrents = (
        _torrent(
            "f" * 40,
            tracker="https://public.example.invalid/announce",
            tracker_urls=(
                "https://public.example.invalid/announce",
                "https://tracker.hdtime.org/announce?passkey=abc",
            ),
            comment="https://kp.m-team.cc/detail/123456",
        ),
    )
    service, factory, _ = _setup(tmp_path, site_error=None, torrents=torrents)

    _run(service)

    with factory() as session:
        watch = session.scalars(select(SeedingWatch)).one()
        assert watch.site_key == "hdtime"
        assert watch.identity_source == SeedingIdentitySource.TRACKER.value
        # comment 指向 m-team，ID 属于 m-team；站点是 hdtime，绝不能拿它去 hdtime 探测。
        assert watch.remote_torrent_id is None


def test_comment_torrent_id_kept_when_site_matches(tmp_path: Path) -> None:
    """comment 与 tracker 同站时，站点侧种子 ID 仍然有效。"""

    torrents = (
        _torrent(
            "e" * 40,
            tracker="https://tracker.hdtime.org/announce?passkey=abc",
            comment="https://hdtime.org/details.php?id=998877",
        ),
    )
    service, factory, _ = _setup(tmp_path, site_error=None, torrents=torrents)

    _run(service)

    with factory() as session:
        watch = session.scalars(select(SeedingWatch)).one()
        assert watch.site_key == "hdtime"
        assert watch.identity_source == SeedingIdentitySource.TRACKER.value
        assert watch.remote_torrent_id == "998877"


def test_missing_site_creates_issue(tmp_path: Path) -> None:
    service, factory, _ = _setup(tmp_path, site_error="missing")
    _run(service)
    with factory() as session:
        issues = _issues_of(session, SeedingIssueKind.SITE_MISSING)
        assert len(issues) == 1
        assert issues[0].status == SeedingIssueStatus.OPEN.value
        assert issues[0].suggestion == "RETRIEVE_TORRENT"


def test_unknown_probe_creates_nothing(tmp_path: Path) -> None:
    for error in ("auth", "parse"):
        service, factory, _ = _setup(tmp_path, site_error=error, db_name=f"{error}.db")
        _run(service)
        with factory() as session:
            kinds = [issue.kind for issue in session.scalars(select(SeedingHealthIssue)).all()]
            assert SeedingIssueKind.SITE_MISSING.value not in kinds


def test_confirmation_threshold_blocks_first_miss(tmp_path: Path) -> None:
    factory, _ = _factory(tmp_path / "threshold.db")
    with factory() as session:
        session.add(
            Downloader(
                id="dl-1",
                name="qBittorrent",
                type=DownloaderKind.QBITTORRENT.value,
                base_url="http://127.0.0.1:8080",
                path_mappings=[],
                capabilities={},
                connection_status="OK",
                path_mapping_status="OK",
                enabled=True,
                version=1,
                monitor_rules={},
            )
        )
        session.commit()
    site_adapter = _FakeSiteAdapter(error="missing")
    service = SeedingHealthService(
        factory,
        _FakeDownloaderService(
            (_torrent("b" * 40, tracker_state="ERROR"),), _FakeDownloaderAdapter(site_adapter)
        ),
        _FakeSiteService(site_adapter),
        settings=SeedingHealthSettings(
            confirm_threshold=2, detail_ttl_hours=0, site_probe_enabled=True
        ),
    )
    _run(service)
    with factory() as session:
        watch = session.scalars(select(SeedingWatch)).one()
        assert watch.consecutive_misses == 1
        kinds = [issue.kind for issue in session.scalars(select(SeedingHealthIssue)).all()]
        assert SeedingIssueKind.SITE_MISSING.value not in kinds
    _run(service)
    with factory() as session:
        watch = session.scalars(select(SeedingWatch)).one()
        assert watch.consecutive_misses == 2
        assert len(_issues_of(session, SeedingIssueKind.SITE_MISSING)) == 1


def test_file_missing_issue_and_auto_clear(tmp_path: Path) -> None:
    mappings = [PathMappingRule(remote_prefix="/downloads", container_prefix=str(tmp_path))]
    missing = _torrent("c" * 40, content_path="/downloads/not-there.mkv")
    service, factory, _ = _setup(tmp_path, site_error=None, torrents=(missing,), mappings=mappings)
    _run(service)
    with factory() as session:
        issues = session.scalars(select(SeedingHealthIssue)).all()
        assert len(issues) == 1
        assert issues[0].kind == SeedingIssueKind.FILE_MISSING.value

    (tmp_path / "not-there.mkv").write_bytes(b"x")
    _run(service)
    with factory() as session:
        issue = session.scalars(select(SeedingHealthIssue)).one()
        assert issue.status == SeedingIssueStatus.RESOLVED.value
        assert issue.resolved_reason == "AUTO"


def test_tracker_error_issue(tmp_path: Path) -> None:
    service, factory, _ = _setup(
        tmp_path, site_error=None, torrents=(_torrent("d" * 40, tracker_state="ERROR"),)
    )
    _run(service)
    with factory() as session:
        issues = session.scalars(select(SeedingHealthIssue)).all()
        assert [issue.kind for issue in issues] == [SeedingIssueKind.TRACKER_ERROR.value]


def test_gone_torrent_resolves_issues(tmp_path: Path) -> None:
    service, factory, _ = _setup(tmp_path, site_error="missing")
    _run(service)
    with factory() as session:
        assert len(_issues_of(session, SeedingIssueKind.SITE_MISSING)) == 1

    service._downloaders._torrents = ()  # type: ignore[attr-defined]
    _run(service)
    with factory() as session:
        issue = _issues_of(session, SeedingIssueKind.SITE_MISSING)[0]
        assert issue.status == SeedingIssueStatus.RESOLVED.value
        assert issue.resolved_reason == "GONE"


def test_summary_counts(tmp_path: Path) -> None:
    service, factory, _ = _setup(tmp_path, site_error="missing")
    _run(service)
    summary = service.summary()
    assert summary["watched"] == 1
    assert summary["abnormal"] == 1
    assert summary["healthy"] == 0
    assert summary["by_kind"][SeedingIssueKind.SITE_MISSING.value] == 1
    # tracker 报错的种子同时产生 SITE_MISSING 与 TRACKER_ERROR 两条。
    assert summary["pending"] == 2
    assert summary["stale"] is False


def test_issue_actions(tmp_path: Path) -> None:
    service, factory, _ = _setup(tmp_path, site_error="missing")
    _run(service)
    with factory() as session:
        issue_id = _issues_of(session, SeedingIssueKind.SITE_MISSING)[0].id
    asyncio.run(service.act(issue_id, "ACK"))
    assert service.issue_detail(issue_id)["status"] == SeedingIssueStatus.ACKED.value
    asyncio.run(service.act(issue_id, "IGNORE"))
    assert service.issue_detail(issue_id)["status"] == SeedingIssueStatus.IGNORED.value


def test_site_only_detects_torrent_absent_from_downloader(tmp_path: Path) -> None:
    local_ids = ("100001", "100002", "100003", "100004", "100005")
    torrents = tuple(
        _torrent(
            chr(ord("a") + index) * 40,
            comment=f"https://hdtime.org/details.php?id={torrent_id}",
        )
        for index, torrent_id in enumerate(local_ids)
    )
    service, factory, site_adapter = _setup(tmp_path, site_error=None, torrents=torrents)
    site_adapter.seeding_ids = (*local_ids, "999999")

    _run(service)

    with factory() as session:
        issues = _issues_of(session, SeedingIssueKind.SITE_ONLY)
        assert len(issues) == 1
        assert "999999" in issues[0].evidence
        assert issues[0].status == SeedingIssueStatus.OPEN.value
        watch = session.get(SeedingWatch, issues[0].watch_id)
        assert watch is not None
        assert watch.downloader_id is None
        assert watch.presence == SeedingWatchPresence.SITE_ONLY.value
        assert watch.site_key == "hdtime"
        assert watch.remote_torrent_id == "999999"


def test_site_only_ignores_transseeded_torrent_without_site_id(tmp_path: Path) -> None:
    """转种种子没有站点侧 ID，不能因此把站侧对应条目误报成「站侧独有」。"""

    local_ids = ("100001", "100002", "100003", "100004", "100005")
    torrents = tuple(
        _torrent(
            chr(ord("a") + index) * 40,
            comment=f"https://hdtime.org/details.php?id={torrent_id}",
        )
        for index, torrent_id in enumerate(local_ids)
    ) + (
        _torrent(
            "t" * 40,
            tracker="https://tracker.hdtime.org/announce?passkey=abc",
            comment="https://kp.m-team.cc/detail/777777",
        ),
    )
    service, factory, site_adapter = _setup(tmp_path, site_error=None, torrents=torrents)

    class _TitledAdapter(_FakeSiteAdapter):
        async def list_seeding(self, cursor: str | None = None) -> SiteSeedingList:
            return SiteSeedingList(
                supported=True,
                items=(
                    SiteSeedingItem(torrent_id="777777", title="Dune Part Two 2024 2160p"),
                    SiteSeedingItem(torrent_id="999999", title="Other Movie 2023 1080p"),
                ),
                next_cursor=None,
                total_hint=2,
            )

    site_adapter.__class__ = _TitledAdapter

    _run(service)

    with factory() as session:
        issues = _issues_of(session, SeedingIssueKind.SITE_ONLY)
        assert [issue.watch_id for issue in issues]
        assert all("999999" in issue.evidence for issue in issues)
        assert len(issues) == 1


def test_site_only_skips_when_site_list_is_incomplete(tmp_path: Path) -> None:
    service, factory, site_adapter = _setup(tmp_path, site_error=None)

    class _PartialAdapter(_FakeSiteAdapter):
        async def list_seeding(self, cursor: str | None = None) -> SiteSeedingList:
            return SiteSeedingList(
                supported=True,
                items=(SiteSeedingItem(torrent_id="999999", title="站点种子"),),
                next_cursor="1",
                total_hint=5000,
            )

    site_adapter.__class__ = _PartialAdapter

    _run(service)

    with factory() as session:
        assert _issues_of(session, SeedingIssueKind.SITE_ONLY) == []


def test_site_only_skips_when_local_has_no_site_identity(tmp_path: Path) -> None:
    """本地一条该站身份都没识别出时，差集等于「未识别」，必须放弃判定。"""
    service, factory, site_adapter = _setup(
        tmp_path, site_error=None, torrents=(_torrent("a" * 40, comment=None),)
    )
    site_adapter.seeding_ids = ("999999", "1000000")

    _run(service)

    with factory() as session:
        assert _issues_of(session, SeedingIssueKind.SITE_ONLY) == []


def test_site_only_resolves_when_torrent_returns_to_downloader(tmp_path: Path) -> None:
    local_ids = ("100001", "100002", "100003", "100004", "100005")
    torrents = tuple(
        _torrent(
            chr(ord("a") + index) * 40,
            comment=f"https://hdtime.org/details.php?id={torrent_id}",
        )
        for index, torrent_id in enumerate(local_ids)
    )
    service, factory, site_adapter = _setup(tmp_path, site_error=None, torrents=torrents)
    site_adapter.seeding_ids = (*local_ids, "999999")
    _run(service)
    with factory() as session:
        assert len(_issues_of(session, SeedingIssueKind.SITE_ONLY)) == 1

    # 该种子被重新加回下载器，下一轮应自动恢复历史异常。
    cast(Any, service._downloaders)._torrents = torrents + (
        _torrent("z" * 40, comment="https://hdtime.org/details.php?id=999999"),
    )
    _run(service)
    with factory() as session:
        issues = _issues_of(session, SeedingIssueKind.SITE_ONLY)
        assert len(issues) == 1
        assert issues[0].status == SeedingIssueStatus.RESOLVED.value
