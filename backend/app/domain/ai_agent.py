from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class AIProviderKind(StrEnum):
    OPENAI = "OPENAI"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"


class AIConnectionStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


class AIDataScope(StrEnum):
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
    TASK_EXECUTIONS = "TASK_EXECUTIONS"
    REDACTED_LOGS = "REDACTED_LOGS"
    SITE_STATUS = "SITE_STATUS"
    DOWNLOADER_STATUS = "DOWNLOADER_STATUS"
    VERSION_STATUS = "VERSION_STATUS"
    HELP_DOCS = "HELP_DOCS"


class AIToolName(StrEnum):
    GET_SYSTEM_HEALTH = "get_system_health"
    GET_VERSION_STATUS = "get_version_status"
    LIST_TASK_DEFINITIONS = "list_task_definitions"
    GET_TASK_EXECUTION = "get_task_execution"
    QUERY_REDACTED_LOGS = "query_redacted_logs"
    GET_SITE_STATUS = "get_site_status"
    GET_DOWNLOADER_STATUS = "get_downloader_status"
    SEARCH_HELP_DOCS = "search_help_docs"


AI_TOOL_SCOPES: dict[AIToolName, AIDataScope] = {
    AIToolName.GET_SYSTEM_HEALTH: AIDataScope.SYSTEM_HEALTH,
    AIToolName.GET_VERSION_STATUS: AIDataScope.VERSION_STATUS,
    AIToolName.LIST_TASK_DEFINITIONS: AIDataScope.TASK_EXECUTIONS,
    AIToolName.GET_TASK_EXECUTION: AIDataScope.TASK_EXECUTIONS,
    AIToolName.QUERY_REDACTED_LOGS: AIDataScope.REDACTED_LOGS,
    AIToolName.GET_SITE_STATUS: AIDataScope.SITE_STATUS,
    AIToolName.GET_DOWNLOADER_STATUS: AIDataScope.DOWNLOADER_STATUS,
    AIToolName.SEARCH_HELP_DOCS: AIDataScope.HELP_DOCS,
}


DEFAULT_AI_DATA_SCOPES: tuple[AIDataScope, ...] = tuple(AIDataScope)


def normalize_ai_base_url(kind: AIProviderKind, value: str | None) -> str:
    raw = (value or "").strip()
    if kind is AIProviderKind.OPENAI and not raw:
        return OPENAI_DEFAULT_BASE_URL
    if not raw:
        raise ValueError("OpenAI-compatible Base URL 不能为空")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AI Provider Base URL 无效") from exc
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("AI Provider Base URL 只允许 HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("AI Provider Base URL 不能包含账号或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("AI Provider Base URL 不能包含 query 或 fragment")
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    normalized = urlunsplit((parsed.scheme.lower(), host, path, "", ""))
    if kind is AIProviderKind.OPENAI and normalized != OPENAI_DEFAULT_BASE_URL:
        raise ValueError("OpenAI Provider 只能使用官方 API Base URL")
    return normalized


def normalize_ai_model(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("AI Model 不能为空且最长 200 个字符")
    if any(char in normalized for char in ("\r", "\n", "\x00")):
        raise ValueError("AI Model 不能包含控制字符")
    return normalized
