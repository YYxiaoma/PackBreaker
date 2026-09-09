from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from backend.app.api.dependencies import AccessPrincipal, require_admin_or_scope
from backend.app.domain.auth import ApiScope
from backend.app.infrastructure.persistence.models import UnpackTask
from backend.app.infrastructure.runtime import RuntimeManager

router = APIRouter(tags=["system"])
CONFIG_READ_ACCESS = require_admin_or_scope(ApiScope.CONFIG_READ)


@router.get("/system/status")
async def system_status(
    request: Request,
    principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> dict[str, object]:
    runtime = cast(RuntimeManager, request.app.state.runtime)
    with runtime.session_factory() as session:
        rows = session.execute(
            select(UnpackTask.status, func.count()).group_by(UnpackTask.status)
        ).all()
    by_status = {status: count for status, count in rows}
    return {
        "version": "0.1.0",
        "authenticated_via": principal.kind,
        "tasks": {"total": sum(by_status.values()), "by_status": by_status},
        "checks": runtime.readiness().as_dict()["checks"],
    }
