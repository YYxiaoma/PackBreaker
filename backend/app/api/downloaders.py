from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    downloader_service,
    require_admin_csrf_or_scope,
    require_admin_or_scope,
)
from backend.app.application.downloaders import (
    DownloaderUpdate,
    DownloaderView,
    PathDiagnosticProbe,
    PathDiagnosticReport,
    PathDiagnosticResult,
)
from backend.app.application.errors import ApplicationError
from backend.app.domain.auth import ApiScope
from backend.app.domain.downloader import DownloaderCredential, PathMappingRule
from backend.app.domain.verification import DownloaderKind

router = APIRouter(tags=["downloaders"])
CONFIG_READ_ACCESS = require_admin_or_scope(ApiScope.CONFIG_READ)
CONFIG_WRITE_ACCESS = require_admin_csrf_or_scope(ApiScope.CONFIG_WRITE)


class DownloaderCredentialInput(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)


class PathMappingInput(BaseModel):
    remote_prefix: str = Field(min_length=1, max_length=4096)
    container_prefix: str = Field(min_length=1, max_length=4096)


class DownloaderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: DownloaderKind
    base_url: str = Field(min_length=1, max_length=2048)
    credential: DownloaderCredentialInput | None = None
    monitor_rules: dict[str, Any] = Field(default_factory=dict)
    path_mappings: list[PathMappingInput] = Field(default_factory=list, max_length=64)


class DownloaderPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    type: DownloaderKind | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    credential: DownloaderCredentialInput | None = None
    clear_credential: bool = False
    monitor_rules: dict[str, Any] | None = None
    path_mappings: list[PathMappingInput] | None = Field(default=None, max_length=64)


class PathDiagnosticProbeInput(BaseModel):
    remote_path: str = Field(min_length=1, max_length=8192)
    target_directory: str = Field(min_length=1, max_length=8192)


class PathDiagnosticRequest(BaseModel):
    probes: list[PathDiagnosticProbeInput] = Field(min_length=1, max_length=64)


class DownloaderActionRequest(BaseModel):
    action: Literal["enable", "disable", "refresh_capabilities"]


def _credential(value: DownloaderCredentialInput | None) -> DownloaderCredential | None:
    if value is None:
        return None
    return DownloaderCredential(
        username=value.username,
        password=value.password.get_secret_value() if value.password is not None else None,
        api_key=value.api_key.get_secret_value() if value.api_key is not None else None,
    )


def _mappings(values: list[PathMappingInput]) -> list[PathMappingRule]:
    return [
        PathMappingRule(
            remote_prefix=value.remote_prefix,
            container_prefix=value.container_prefix,
        )
        for value in values
    ]


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _view(record: DownloaderView) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "type": record.type.value,
        "base_url": record.base_url,
        "credential_configured": record.credential_configured,
        "monitor_rules": record.monitor_rules,
        "path_mappings": [
            {
                "remote_prefix": mapping.remote_prefix,
                "container_prefix": mapping.container_prefix,
            }
            for mapping in record.path_mappings
        ],
        "capabilities": record.capabilities,
        "connection_status": record.connection_status.value,
        "path_mapping_status": record.path_mapping_status.value,
        "enabled": record.enabled,
        "version": record.version,
        "last_test_at": _timestamp(record.last_test_at),
        "last_path_diagnostic_at": _timestamp(record.last_path_diagnostic_at),
        "created_at": _timestamp(record.created_at),
        "updated_at": _timestamp(record.updated_at),
    }


def _diagnostic_result(result: PathDiagnosticResult) -> dict[str, object]:
    return {
        "status": "ok" if result.ok else "blocked",
        "rule_index": result.rule_index,
        "container_visible": result.container_visible,
        "regular_file": result.regular_file,
        "readable": result.readable,
        "target_writable": result.target_writable,
        "round_trip": result.round_trip,
        "source_device": result.source_device,
        "target_device": result.target_device,
        "same_device": result.same_device,
        "hardlink_feasible": result.hardlink_feasible,
        "error_code": result.error_code,
    }


def _diagnostic(report: PathDiagnosticReport) -> dict[str, object]:
    return {
        "status": "ok" if report.ok else "blocked",
        "all_mappings_verified": report.all_mappings_verified,
        "error_code": report.error_code,
        "results": [_diagnostic_result(result) for result in report.results],
    }


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="修改或删除下载器时必须提供 If-Match 版本",
        )
    if len(value) < 3 or not value.startswith('"') or not value.endswith('"'):
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail='If-Match 必须使用形如 "3" 的强 ETag',
        )
    try:
        parsed = int(value[1:-1])
    except ValueError as exc:
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail='If-Match 必须使用形如 "3" 的强 ETag',
        ) from exc
    if parsed < 1:
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail="下载器版本必须大于等于 1",
        )
    return parsed


def _json_with_etag(record: DownloaderView) -> JSONResponse:
    return JSONResponse(_view(record), headers={"ETag": f'"{record.version}"'})


@router.get("/downloaders")
async def list_downloaders(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> dict[str, object]:
    return {"items": [_view(item) for item in downloader_service(request).list_downloaders()]}


@router.post("/downloaders", status_code=status.HTTP_201_CREATED)
async def create_downloader(
    request: Request,
    payload: DownloaderCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> JSONResponse:
    created = downloader_service(request).create(
        name=payload.name,
        kind=payload.type,
        base_url=payload.base_url,
        credential=_credential(payload.credential),
        monitor_rules=payload.monitor_rules,
        path_mappings=_mappings(payload.path_mappings),
    )
    response = _json_with_etag(created)
    response.status_code = status.HTTP_201_CREATED
    return response


@router.get("/downloaders/{downloader_id}")
async def get_downloader(
    downloader_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    return _json_with_etag(downloader_service(request).get(downloader_id))


@router.patch("/downloaders/{downloader_id}")
async def patch_downloader(
    downloader_id: str,
    request: Request,
    payload: DownloaderPatchRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    if payload.clear_credential and payload.credential is not None:
        raise ApplicationError(
            code="DOWNLOADER_CREDENTIAL_INVALID",
            status=422,
            title="下载器凭证操作冲突",
            detail="credential 与 clear_credential 不能同时使用",
        )
    credential_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    if payload.clear_credential:
        credential_action = "CLEAR"
    elif "credential" in payload.model_fields_set and payload.credential is not None:
        credential_action = "SET"
    updated = downloader_service(request).update(
        downloader_id,
        expected_version=_expected_version(if_match),
        update_request=DownloaderUpdate(
            name=payload.name,
            type=payload.type,
            base_url=payload.base_url,
            monitor_rules=payload.monitor_rules,
            path_mappings=_mappings(payload.path_mappings)
            if payload.path_mappings is not None
            else None,
            credential_action=credential_action,
            credential=_credential(payload.credential),
        ),
    )
    return _json_with_etag(updated)


@router.delete("/downloaders/{downloader_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_downloader(
    downloader_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    downloader_service(request).delete(
        downloader_id,
        expected_version=_expected_version(if_match),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/downloaders/{downloader_id}/test")
async def test_downloader(
    downloader_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    return await downloader_service(request).test_connection(downloader_id)


@router.post("/downloaders/{downloader_id}/path-diagnostics")
async def diagnose_downloader_path(
    downloader_id: str,
    request: Request,
    payload: PathDiagnosticRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    result = await downloader_service(request).path_diagnostics(
        downloader_id,
        probes=[
            PathDiagnosticProbe(
                remote_path=probe.remote_path,
                target_directory=Path(probe.target_directory),
            )
            for probe in payload.probes
        ],
    )
    return _diagnostic(result)


@router.get("/downloaders/{downloader_id}/tasks")
async def list_downloader_tasks(
    downloader_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> dict[str, object]:
    return {"items": downloader_service(request).list_tasks(downloader_id)}


@router.post("/downloaders/{downloader_id}/actions")
async def downloader_action(
    downloader_id: str,
    request: Request,
    payload: DownloaderActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    if payload.action == "refresh_capabilities":
        result = await downloader_service(request).test_connection(downloader_id)
        return JSONResponse(result)
    record = downloader_service(request).set_enabled(
        downloader_id,
        expected_version=_expected_version(if_match),
        enabled=payload.action == "enable",
    )
    return _json_with_etag(record)
