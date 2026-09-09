from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies import (
    api_token_service,
    require_admin_csrf,
    require_admin_session,
)
from backend.app.application.auth import AuthIdentity
from backend.app.application.automation_access import ApiTokenCreated, ApiTokenView
from backend.app.domain.auth import ApiScope

router = APIRouter(tags=["auth"])


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[ApiScope] = Field(min_length=1)
    expires_at: datetime


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _view(record: ApiTokenView) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "scopes": [scope.value for scope in record.scopes],
        "expires_at": _timestamp(record.expires_at),
        "revoked_at": _timestamp(record.revoked_at),
        "created_at": _timestamp(record.created_at),
    }


@router.get("/api-tokens")
async def list_api_tokens(
    request: Request,
    _identity: Annotated[AuthIdentity, Depends(require_admin_session)],
) -> dict[str, object]:
    return {"items": [_view(item) for item in api_token_service(request).list()]}


@router.post("/api-tokens", status_code=status.HTTP_201_CREATED)
async def create_api_token(
    request: Request,
    payload: ApiTokenCreateRequest,
    _identity: Annotated[AuthIdentity, Depends(require_admin_csrf)],
) -> dict[str, object]:
    created: ApiTokenCreated = api_token_service(request).create(
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
    )
    return {
        "id": created.id,
        "name": created.name,
        "token": created.token,
        "scopes": [scope.value for scope in created.scopes],
        "expires_at": _timestamp(created.expires_at),
        "created_at": _timestamp(created.created_at),
    }


@router.delete("/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_token(
    token_id: str,
    request: Request,
    _identity: Annotated[AuthIdentity, Depends(require_admin_csrf)],
) -> Response:
    api_token_service(request).revoke(token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
