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


class ApiTokenViewResponse(BaseModel):
    id: str
    name: str
    scopes: list[ApiScope]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class ApiTokenListResponse(BaseModel):
    items: list[ApiTokenViewResponse]


class ApiTokenCreatedResponse(BaseModel):
    id: str
    name: str
    token: str
    scopes: list[ApiScope]
    expires_at: datetime
    created_at: datetime


def _view(record: ApiTokenView) -> ApiTokenViewResponse:
    return ApiTokenViewResponse(
        id=record.id,
        name=record.name,
        scopes=list(record.scopes),
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        created_at=record.created_at,
    )


@router.get("/api-tokens")
async def list_api_tokens(
    request: Request,
    _identity: Annotated[AuthIdentity, Depends(require_admin_session)],
) -> ApiTokenListResponse:
    return ApiTokenListResponse(items=[_view(item) for item in api_token_service(request).list()])


@router.post("/api-tokens", status_code=status.HTTP_201_CREATED)
async def create_api_token(
    request: Request,
    payload: ApiTokenCreateRequest,
    _identity: Annotated[AuthIdentity, Depends(require_admin_csrf)],
) -> ApiTokenCreatedResponse:
    created: ApiTokenCreated = api_token_service(request).create(
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
    )
    return ApiTokenCreatedResponse(
        id=created.id,
        name=created.name,
        token=created.token,
        scopes=list(created.scopes),
        expires_at=created.expires_at,
        created_at=created.created_at,
    )


@router.delete("/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_token(
    token_id: str,
    request: Request,
    _identity: Annotated[AuthIdentity, Depends(require_admin_csrf)],
) -> Response:
    api_token_service(request).revoke(token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
