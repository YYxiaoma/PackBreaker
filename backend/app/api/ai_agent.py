from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    ai_agent_service,
    ai_telegram_service,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.ai_agent import (
    AIAgentProbeInput,
    AIAgentSettingUpdate,
    AIAgentSettingView,
)
from backend.app.application.ai_telegram import (
    AITelegramBindingUpdate,
    AITelegramBindingView,
)
from backend.app.application.ai_telegram_driver import AITelegramDriver
from backend.app.application.errors import ApplicationError
from backend.app.domain.ai_agent import AIConnectionStatus, AIDataScope, AIProviderKind

router = APIRouter(tags=["ai-agent"])
CONFIG_READ_ACCESS = require_admin_principal
CONFIG_WRITE_ACCESS = require_admin_csrf_principal


class AIAgentSettingResponse(BaseModel):
    enabled: bool
    provider_kind: AIProviderKind
    base_url: str
    api_key_configured: bool
    model: str
    request_timeout_seconds: int
    max_context_messages: int
    data_scopes: list[AIDataScope]
    connection_status: AIConnectionStatus
    last_test_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class AIAgentSettingUpdateRequest(BaseModel):
    enabled: bool
    provider_kind: AIProviderKind
    base_url: str | None = Field(default=None, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_context_messages: int = Field(default=20, ge=2, le=100)
    data_scopes: list[AIDataScope] = Field(min_length=1)
    api_key_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    api_key: SecretStr | None = Field(default=None, max_length=8192)


class AIAgentProbeRequest(BaseModel):
    use_saved: bool = False
    provider_kind: AIProviderKind | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    api_key: SecretStr | None = Field(default=None, max_length=8192)


class AIAgentProbeResponse(BaseModel):
    status: Literal["ok"]
    provider_kind: AIProviderKind
    model: str
    tested_at: datetime


class AIAgentStatusResponse(BaseModel):
    enabled: bool
    provider_kind: AIProviderKind
    connection_status: AIConnectionStatus
    api_key_configured: bool
    model: str
    last_test_at: datetime | None
    telegram_enabled: bool
    telegram_approval_enabled: bool
    telegram_driver_running: bool
    telegram_last_update_id: int
    telegram_consecutive_errors: int
    telegram_last_error_code: str | None


class AITelegramBindingResponse(BaseModel):
    notification_channel_id: str | None
    enabled: bool
    approval_enabled: bool
    allowed_chat_ids: list[str]
    allowed_user_ids: list[str]
    idle_timeout_minutes: int
    max_context_messages: int
    last_update_id: int
    version: int
    created_at: datetime
    updated_at: datetime


class AITelegramBindingUpdateRequest(BaseModel):
    notification_channel_id: str | None = Field(default=None, max_length=36)
    enabled: bool = False
    approval_enabled: bool = False
    allowed_chat_ids: list[str] = Field(default_factory=list, max_length=100)
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=100)
    idle_timeout_minutes: int = Field(default=60, ge=5, le=10080)
    max_context_messages: int = Field(default=20, ge=2, le=100)


def _view(record: AIAgentSettingView) -> AIAgentSettingResponse:
    return AIAgentSettingResponse(
        enabled=record.enabled,
        provider_kind=record.provider_kind,
        base_url=record.base_url,
        api_key_configured=record.api_key_configured,
        model=record.model,
        request_timeout_seconds=record.request_timeout_seconds,
        max_context_messages=record.max_context_messages,
        data_scopes=list(record.data_scopes),
        connection_status=record.connection_status,
        last_test_at=record.last_test_at,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="修改 AI 助手配置时必须提供 If-Match 版本",
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
            detail="AI 助手配置版本必须大于等于 1",
        )
    return parsed


def _response(record: AIAgentSettingView) -> JSONResponse:
    response = _view(record)
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{record.version}"', "Cache-Control": "no-store"},
    )


def _telegram_view(record: AITelegramBindingView) -> AITelegramBindingResponse:
    return AITelegramBindingResponse(
        notification_channel_id=record.notification_channel_id,
        enabled=record.enabled,
        approval_enabled=record.approval_enabled,
        allowed_chat_ids=list(record.allowed_chat_ids),
        allowed_user_ids=list(record.allowed_user_ids),
        idle_timeout_minutes=record.idle_timeout_minutes,
        max_context_messages=record.max_context_messages,
        last_update_id=record.last_update_id,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _telegram_response(record: AITelegramBindingView) -> JSONResponse:
    response = _telegram_view(record)
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{record.version}"', "Cache-Control": "no-store"},
    )


@router.get("/ai-agent/settings", response_model=AIAgentSettingResponse)
async def get_ai_agent_settings(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    return _response(ai_agent_service(request).get())


@router.put("/ai-agent/settings", response_model=AIAgentSettingResponse)
async def update_ai_agent_settings(
    request: Request,
    payload: AIAgentSettingUpdateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    updated = ai_agent_service(request).update(
        AIAgentSettingUpdate(
            enabled=payload.enabled,
            provider_kind=payload.provider_kind,
            base_url=payload.base_url,
            model=payload.model,
            request_timeout_seconds=payload.request_timeout_seconds,
            max_context_messages=payload.max_context_messages,
            data_scopes=tuple(payload.data_scopes),
            api_key_action=payload.api_key_action,
            api_key=payload.api_key.get_secret_value() if payload.api_key is not None else None,
        ),
        expected_version=_expected_version(if_match),
    )
    return _response(updated)


@router.post("/ai-agent/test", response_model=AIAgentProbeResponse)
async def test_ai_agent_provider(
    request: Request,
    payload: AIAgentProbeRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> AIAgentProbeResponse:
    service = ai_agent_service(request)
    if payload.use_saved:
        if any(
            value is not None
            for value in (
                payload.provider_kind,
                payload.base_url,
                payload.model,
                payload.request_timeout_seconds,
                payload.api_key,
            )
        ):
            raise ApplicationError(
                code="AI_AGENT_CONFIG_INVALID",
                status=422,
                title="AI 助手配置无效",
                detail="测试已保存配置时不能同时提交临时 Provider 参数",
            )
        saved = await service.probe_saved()
        assert saved.last_test_at is not None
        return AIAgentProbeResponse(
            status="ok",
            provider_kind=saved.provider_kind,
            model=saved.model,
            tested_at=saved.last_test_at,
        )
    if payload.provider_kind is None or payload.model is None:
        raise ApplicationError(
            code="AI_AGENT_CONFIG_INVALID",
            status=422,
            title="AI 助手配置无效",
            detail="临时测试必须提供 Provider 类型和 Model",
        )
    result = await service.probe_temporary(
        AIAgentProbeInput(
            provider_kind=payload.provider_kind,
            base_url=payload.base_url,
            model=payload.model,
            request_timeout_seconds=payload.request_timeout_seconds or 30,
            api_key=payload.api_key.get_secret_value() if payload.api_key is not None else None,
        )
    )
    return AIAgentProbeResponse(
        status="ok",
        provider_kind=result.provider_kind,
        model=result.model,
        tested_at=result.tested_at,
    )


@router.get("/ai-agent/status", response_model=AIAgentStatusResponse)
async def get_ai_agent_status(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> AIAgentStatusResponse:
    current = ai_agent_service(request).get()
    telegram = ai_telegram_service(request).get()
    driver = cast(AITelegramDriver, request.app.state.ai_telegram_driver)
    driver_state = driver.state
    return AIAgentStatusResponse(
        enabled=current.enabled,
        provider_kind=current.provider_kind,
        connection_status=current.connection_status,
        api_key_configured=current.api_key_configured,
        model=current.model,
        last_test_at=current.last_test_at,
        telegram_enabled=telegram.enabled,
        telegram_approval_enabled=telegram.approval_enabled,
        telegram_driver_running=driver_state.running,
        telegram_last_update_id=telegram.last_update_id,
        telegram_consecutive_errors=driver_state.consecutive_errors,
        telegram_last_error_code=driver_state.last_error_code,
    )


@router.get("/ai-agent/telegram", response_model=AITelegramBindingResponse)
async def get_ai_telegram_binding(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    return _telegram_response(ai_telegram_service(request).get())


@router.put("/ai-agent/telegram", response_model=AITelegramBindingResponse)
async def update_ai_telegram_binding(
    request: Request,
    payload: AITelegramBindingUpdateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    updated = ai_telegram_service(request).update(
        AITelegramBindingUpdate(
            notification_channel_id=payload.notification_channel_id,
            enabled=payload.enabled,
            approval_enabled=payload.approval_enabled,
            allowed_chat_ids=tuple(payload.allowed_chat_ids),
            allowed_user_ids=tuple(payload.allowed_user_ids),
            idle_timeout_minutes=payload.idle_timeout_minutes,
            max_context_messages=payload.max_context_messages,
        ),
        expected_version=_expected_version(if_match),
    )
    return _telegram_response(updated)
