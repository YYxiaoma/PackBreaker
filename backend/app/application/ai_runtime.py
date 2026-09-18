from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_tools import AIToolService
from backend.app.application.errors import ApplicationError
from backend.app.domain.ai_agent import AIToolName
from backend.app.infrastructure.adapters.ai_provider import AIProviderError, AIProviderToolCall

_MAX_TOOL_ROUNDS = 4
_MAX_TOOL_CALLS = 8
_MAX_TOOL_RESULT_CHARS = 64 * 1024
_MAX_USER_MESSAGE_CHARS = 4096
_MAX_ASSISTANT_MESSAGE_CHARS = 8192

_SYSTEM_PROMPT = (
    "你是 PackBreaker 的只读运维助手。只能依据提供的只读工具和对话内容回答。"
    "不得要求或推断 Cookie、API Key、密码、Session/CSRF、代理密码等秘密。"
    "不得声称执行了拆包、修改配置、删除记录、修改密码或升级等副作用。"
    "如果用户需要写操作，只能说明应到现有 UI 中人工确认。"
)


@dataclass(frozen=True, slots=True)
class AIHistoryMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class AIReadOnlyAnswer:
    content: str
    tool_summary: tuple[str, ...]
    context_truncated: bool


class AIReadOnlyAgent:
    def __init__(self, settings_service: AIAgentService, tool_service: AIToolService) -> None:
        self._settings_service = settings_service
        self._tool_service = tool_service

    async def respond(
        self,
        *,
        history: tuple[AIHistoryMessage, ...],
        user_message: str,
    ) -> AIReadOnlyAnswer:
        normalized_user = user_message.strip()
        if not normalized_user or len(normalized_user) > _MAX_USER_MESSAGE_CHARS:
            raise ApplicationError(
                code="AI_MESSAGE_INVALID",
                status=422,
                title="AI 对话消息无效",
                detail="消息不能为空且最长 4096 个字符",
            )
        runtime = self._settings_service.runtime_config()
        bounded_history = history[-runtime.max_context_messages :]
        context_truncated = len(history) > len(bounded_history)
        messages: list[dict[str, object]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        for item in bounded_history:
            if item.role not in {"user", "assistant"} or not item.content:
                continue
            messages.append(
                {
                    "role": item.role,
                    "content": item.content[:_MAX_ASSISTANT_MESSAGE_CHARS],
                }
            )
        messages.append({"role": "user", "content": normalized_user})
        tools = _tool_definitions(self._tool_service.available_tools(runtime.data_scopes))
        summary: list[str] = []
        tool_calls_used = 0

        for _round in range(_MAX_TOOL_ROUNDS + 1):
            try:
                turn = await runtime.provider.chat(messages=messages, tools=tools)
            except AIProviderError as exc:
                raise self._provider_error(exc) from exc
            if not turn.tool_calls:
                content = (turn.content or "").strip()
                if not content:
                    raise self._provider_invalid("Provider 未返回可用的文本回答")
                return AIReadOnlyAnswer(
                    content=content[:_MAX_ASSISTANT_MESSAGE_CHARS],
                    tool_summary=tuple(summary),
                    context_truncated=context_truncated,
                )
            if tool_calls_used + len(turn.tool_calls) > _MAX_TOOL_CALLS:
                raise self._provider_invalid("Provider 请求的工具调用次数超过安全上限")
            tool_calls_used += len(turn.tool_calls)
            messages.append(_assistant_tool_message(turn.content, turn.tool_calls))
            for call in turn.tool_calls:
                tool_name = self._tool_name(call.name)
                arguments = self._tool_arguments(call.arguments_json)
                result = await self._tool_service.execute(
                    tool_name,
                    arguments,
                    allowed_scopes=runtime.data_scopes,
                )
                serialized = json.dumps(
                    result.payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                if len(serialized) > _MAX_TOOL_RESULT_CHARS:
                    serialized = serialized[:_MAX_TOOL_RESULT_CHARS]
                    context_truncated = True
                summary.append(tool_name.value)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": serialized})
        raise self._provider_invalid("Provider 未在有限工具轮次内生成最终回答")

    @staticmethod
    def _tool_name(value: str) -> AIToolName:
        try:
            return AIToolName(value)
        except ValueError as exc:
            raise ApplicationError(
                code="AI_TOOL_UNAUTHORIZED",
                status=502,
                title="Provider 请求了未授权工具",
                detail="模型请求的工具不在 PackBreaker 只读白名单中",
            ) from exc

    @staticmethod
    def _tool_arguments(value: str) -> dict[str, object]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AIReadOnlyAgent._provider_invalid(
                "Provider 返回的 Tool 参数不是有效 JSON"
            ) from exc
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise AIReadOnlyAgent._provider_invalid("Provider 返回的 Tool 参数必须是 JSON Object")
        return cast(dict[str, object], payload)

    @staticmethod
    def _provider_error(exc: AIProviderError) -> ApplicationError:
        return ApplicationError(
            code=exc.code,
            status=502,
            title="AI Provider 调用失败",
            detail="AI Provider 暂时不可用或返回无效响应",
        )

    @staticmethod
    def _provider_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="AI_PROVIDER_INVALID_RESPONSE",
            status=502,
            title="AI Provider 响应无效",
            detail=detail,
        )


def _assistant_tool_message(
    content: str | None,
    calls: tuple[AIProviderToolCall, ...],
) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in calls
        ],
    }


def _tool_definitions(names: tuple[AIToolName, ...]) -> list[dict[str, object]]:
    schemas: dict[AIToolName, tuple[str, dict[str, object]]] = {
        AIToolName.GET_SYSTEM_HEALTH: ("读取 PackBreaker 当前系统健康摘要。", _object_schema()),
        AIToolName.GET_VERSION_STATUS: ("读取当前版本与更新状态。", _object_schema()),
        AIToolName.LIST_TASK_DEFINITIONS: (
            "列出任务定义和最近执行摘要，不返回目录路径或配置快照。",
            _object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        ),
        AIToolName.GET_TASK_EXECUTION: (
            "读取指定任务执行的脱敏摘要、结果和事件。",
            _object_schema({"execution_id": {"type": "string", "maxLength": 36}}, ["execution_id"]),
        ),
        AIToolName.QUERY_REDACTED_LOGS: (
            "查询有限窗口内的已脱敏运行日志。",
            _object_schema(
                {
                    "window_minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "level": {
                        "type": "string",
                        "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    },
                    "query": {"type": "string", "maxLength": 128},
                }
            ),
        ),
        AIToolName.GET_SITE_STATUS: (
            "读取站点连接与可靠性状态，不返回凭证。",
            _object_schema(),
        ),
        AIToolName.GET_DOWNLOADER_STATUS: (
            "读取下载器连接状态和可选运行指标，不返回凭证或路径映射。",
            _object_schema({"include_metrics": {"type": "boolean"}}),
        ),
        AIToolName.SEARCH_HELP_DOCS: (
            "搜索 PackBreaker 打包的 README 与帮助文档。",
            _object_schema({"query": {"type": "string", "maxLength": 80}}, ["query"]),
        ),
    }
    return [
        {
            "type": "function",
            "function": {
                "name": name.value,
                "description": schemas[name][0],
                "parameters": schemas[name][1],
            },
        }
        for name in names
    ]


def _object_schema(
    properties: dict[str, object] | None = None,
    required: list[str] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }
