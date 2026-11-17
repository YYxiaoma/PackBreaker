from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from backend.app.application.sites import SiteService
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import Site, utc_now


def _factory(tmp_path: Path) -> tuple[Any, Any]:
    engine = create_sqlite_engine(tmp_path / "site-refs.db")
    Base.metadata.create_all(engine)
    return create_session_factory(engine), engine


def _add_site(factory: Any, *, site_id: str, name: str, kind: str, enabled: bool) -> None:
    now = utc_now()
    with factory() as session:
        session.add(
            Site(
                id=site_id,
                name=name,
                type=kind,
                base_url="https://pt.keepfrds.com",
                credential_kind="COOKIE",
                enabled=enabled,
                version=1,
                connection_status="UNTESTED",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_template_site_key_matches_canonical_site_id(tmp_path: Path) -> None:
    """模板存的是 SiteKind 大写名，站点引用是适配器小写 ID，必须忽略大小写匹配。"""

    factory, engine = _factory(tmp_path)
    _add_site(factory, site_id="site-keeps", name="KeepFrds", kind="KEEPFRDS", enabled=True)
    try:
        service = SiteService(factory, cast(Any, None))

        upper = service.config_references_for_site_keys(("KEEPFRDS",))
        lower = service.config_references_for_site_keys(("keepfrds",))

        assert [ref.config_id for ref in upper] == ["site-keeps"]
        assert [ref.config_id for ref in lower] == ["site-keeps"]
        # 对外暴露的仍是适配器规范站点 ID，供候选匹配使用。
        assert upper[0].site_id == "keepfrds"
        assert upper[0].enabled is True
    finally:
        engine.dispose()


def test_disabled_site_is_not_reported_as_available(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    _add_site(factory, site_id="site-off", name="KeepFrds", kind="KEEPFRDS", enabled=False)
    try:
        service = SiteService(factory, cast(Any, None))
        assert service.config_references_for_site_keys(("KEEPFRDS",)) == ()
    finally:
        engine.dispose()
