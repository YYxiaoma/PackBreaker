from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.app.infrastructure.runtime import RuntimeManager

router = APIRouter(tags=["system"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    """只证明应用进程能够响应，不探测外部依赖。"""

    return {"status": "ok", "service": "packbreaker"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    """只检查本地关键能力；PT 站和下载器故障不参与容器就绪判断。"""

    runtime = cast(RuntimeManager, request.app.state.runtime)
    report = runtime.readiness()
    payload = report.as_dict()
    if report.ready:
        return JSONResponse(payload)
    return JSONResponse(payload, status_code=503, headers={"Retry-After": "5"})
