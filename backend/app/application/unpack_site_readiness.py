"""Fail-closed check for task-site eligibility at each real execution boundary.

Saving a definition may refer to configured but inactive sites. Starting
manual or scheduled execution is different: every referenced site must
currently be enabled and be part of the production-persistable Registry.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.application.errors import ApplicationError
from backend.app.domain.site_config import SiteKind, site_kind_is_persistable
from backend.app.infrastructure.persistence.models import Site


def require_ready_unpack_sites(session: Session, site_ids: Sequence[str]) -> None:
    if not site_ids:
        raise _not_ready()

    sites = session.scalars(select(Site).where(Site.id.in_(site_ids))).all()
    if len(sites) != len(set(site_ids)):
        raise _not_ready()

    for site in sites:
        if not site.enabled:
            raise _not_ready()
        try:
            kind = SiteKind(site.type)
        except ValueError:
            raise _not_ready() from None
        if not site_kind_is_persistable(kind):
            raise _not_ready()


def _not_ready() -> ApplicationError:
    return ApplicationError(
        code="UNPACK_SITE_NOT_READY",
        status=409,
        title="扫描站点暂不可执行",
        detail="任务包含未启用或尚未通过正式业务验收的扫描站点，请检查站点配置",
    )
