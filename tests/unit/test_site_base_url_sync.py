from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.sites import sync_fixed_site_base_urls
from backend.app.domain.site_config import SiteCredentialKind, SiteKind, trusted_site_base_url
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import Site, utc_now


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def _add_site(
    factory: sessionmaker[Session],
    *,
    site_id: str,
    name: str,
    kind: SiteKind,
    base_url: str,
    connection_status: str = "FAILED",
) -> None:
    now = utc_now()
    with factory() as session:
        session.add(
            Site(
                id=site_id,
                name=name,
                type=kind.value,
                base_url=base_url,
                credential_kind=SiteCredentialKind.COOKIE.value,
                capabilities={},
                connection_status=connection_status,
                enabled=False,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_stale_fixed_base_url_is_synced_to_profile() -> None:
    factory = _factory()
    _add_site(
        factory,
        site_id="site-tangpt",
        name="不可躺",
        kind=SiteKind.TANGPT,
        base_url="https://tangpt.top",
    )

    assert sync_fixed_site_base_urls(factory) == 1

    with factory() as session:
        site = session.get(Site, "site-tangpt")
        assert site is not None
        assert site.base_url == trusted_site_base_url(SiteKind.TANGPT)
        assert site.base_url == "https://www.tangpt.top"
        # 地址变了必须重新探测，不能让旧的 OK 状态继续冒充可用。
        assert site.connection_status == "UNTESTED"
        assert site.version == 2


def test_already_correct_records_are_untouched() -> None:
    factory = _factory()
    _add_site(
        factory,
        site_id="site-carpt",
        name="CarPT",
        kind=SiteKind.CARPT,
        base_url=trusted_site_base_url(SiteKind.CARPT),
        connection_status="OK",
    )

    assert sync_fixed_site_base_urls(factory) == 0

    with factory() as session:
        site = session.get(Site, "site-carpt")
        assert site is not None
        assert site.connection_status == "OK"
        assert site.version == 1


def test_mteam_mirror_is_never_rewritten() -> None:
    factory = _factory()
    _add_site(
        factory,
        site_id="site-mteam",
        name="M-Team",
        kind=SiteKind.MTEAM,
        base_url="https://kp.m-team.cc",
        connection_status="OK",
    )

    assert sync_fixed_site_base_urls(factory) == 0

    with factory() as session:
        site = session.scalar(select(Site).where(Site.id == "site-mteam"))
        assert site is not None
        assert site.base_url == "https://kp.m-team.cc"
