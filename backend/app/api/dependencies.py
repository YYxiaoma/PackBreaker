from dataclasses import dataclass
from typing import Literal, cast

from fastapi import Cookie, Header, Request

from backend.app.application.admin_notifications import AdminNotificationService
from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_telegram import AITelegramService
from backend.app.application.auth import AuthIdentity, AuthService
from backend.app.application.downloaders import DownloaderService
from backend.app.application.notifications import NotificationService
from backend.app.application.sites import SiteService
from backend.app.application.task_actions import TaskActionService
from backend.app.application.task_definition_executions import TaskDefinitionExecutionService
from backend.app.application.task_definitions import TaskDefinitionService
from backend.app.application.task_events import TaskEventService
from backend.app.application.task_operations import TaskOperationService
from backend.app.application.task_repair_actions import TaskRepairActionService
from backend.app.application.task_repairs import TaskRepairPlanService
from backend.app.application.tasks import TaskAnalysisService

SESSION_COOKIE = "packbreaker_session"
CSRF_COOKIE = "packbreaker_csrf"


@dataclass(frozen=True, slots=True)
class AccessPrincipal:
    kind: Literal["admin_session"]
    subject_id: str


def auth_service(request: Request) -> AuthService:
    return cast(AuthService, request.app.state.auth_service)


def downloader_service(request: Request) -> DownloaderService:
    return cast(DownloaderService, request.app.state.downloader_service)


def notification_service(request: Request) -> NotificationService:
    return cast(NotificationService, request.app.state.notification_service)


def admin_notification_service(request: Request) -> AdminNotificationService:
    return cast(AdminNotificationService, request.app.state.admin_notification_service)


def ai_agent_service(request: Request) -> AIAgentService:
    return cast(AIAgentService, request.app.state.ai_agent_service)


def ai_telegram_service(request: Request) -> AITelegramService:
    return cast(AITelegramService, request.app.state.ai_telegram_service)


def site_service(request: Request) -> SiteService:
    return cast(SiteService, request.app.state.site_service)


def task_analysis_service(request: Request) -> TaskAnalysisService:
    return cast(TaskAnalysisService, request.app.state.task_analysis_service)


def task_definition_service(request: Request) -> TaskDefinitionService:
    return cast(TaskDefinitionService, request.app.state.task_definition_service)


def task_definition_execution_service(request: Request) -> TaskDefinitionExecutionService:
    return cast(
        TaskDefinitionExecutionService,
        request.app.state.task_definition_execution_service,
    )


def task_action_service(request: Request) -> TaskActionService:
    return cast(TaskActionService, request.app.state.task_action_service)


def task_event_service(request: Request) -> TaskEventService:
    return cast(TaskEventService, request.app.state.task_event_service)


def task_operation_service(request: Request) -> TaskOperationService:
    return cast(TaskOperationService, request.app.state.task_operation_service)


def task_repair_plan_service(request: Request) -> TaskRepairPlanService:
    return cast(TaskRepairPlanService, request.app.state.task_repair_plan_service)


def task_repair_action_service(request: Request) -> TaskRepairActionService:
    return cast(TaskRepairActionService, request.app.state.task_repair_action_service)


def client_source(request: Request) -> str:
    return cast(str, getattr(request.state, "client_source", "unknown"))


def effective_scheme(request: Request) -> str:
    return cast(str, getattr(request.state, "effective_scheme", request.url.scheme))


async def require_admin_session(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> AuthIdentity:
    return auth_service(request).require_session(session_token)


async def require_admin_csrf(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> AuthIdentity:
    return auth_service(request).require_csrf(
        token=session_token,
        csrf_cookie=csrf_cookie,
        csrf_header=csrf_header,
    )


async def require_admin_principal(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> AccessPrincipal:
    admin = auth_service(request).require_session(session_token)
    return AccessPrincipal("admin_session", admin.session_id)


async def require_admin_csrf_principal(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> AccessPrincipal:
    admin = auth_service(request).require_csrf(
        token=session_token,
        csrf_cookie=csrf_cookie,
        csrf_header=csrf_header,
    )
    return AccessPrincipal("admin_session", admin.session_id)
