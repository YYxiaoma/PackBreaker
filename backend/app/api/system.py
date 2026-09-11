from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from backend.app.api.dependencies import AccessPrincipal, require_admin_or_scope
from backend.app.application.task_driver import ActiveTaskDriver
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
    task_driver = cast(ActiveTaskDriver, request.app.state.task_driver)
    driver_state = task_driver.state
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
        "worker": {
            "running": driver_state.running,
            "ticks_started": driver_state.ticks_started,
            "ticks_completed": driver_state.ticks_completed,
            "ticks_skipped": driver_state.ticks_skipped,
            "consecutive_errors": driver_state.consecutive_errors,
            "last_error_type": driver_state.last_error_type,
            "last_tick_started_at": (
                None
                if driver_state.last_tick_started_at is None
                else driver_state.last_tick_started_at.isoformat()
            ),
            "last_tick_completed_at": (
                None
                if driver_state.last_tick_completed_at is None
                else driver_state.last_tick_completed_at.isoformat()
            ),
            "last_scanned_count": driver_state.last_scanned_count,
            "last_completed_count": driver_state.last_completed_count,
            "last_waiting_count": driver_state.last_waiting_count,
            "last_blocked_count": driver_state.last_blocked_count,
            "last_truncated": driver_state.last_truncated,
        },
    }
