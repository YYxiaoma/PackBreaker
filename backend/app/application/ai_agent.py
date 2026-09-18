from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretNotFound, SecretStore
from backend.app.domain.ai_agent import (
    AIConnectionStatus,
    AIDataScope,
    AIProviderKind,
    normalize_ai_base_url,
    normalize_ai_model,
)
from backend.app.infrastructure.adapters.ai_provider import (
    AIProviderError,
    AIProviderFactory,
    AIProviderProbeResult,
    OpenAICompatibleProvider,
)
from backend.app.infrastructure.persistence.ai_repositories import AIAgentSettingRepository
from backend.app.infrastructure.persistence.models import AIAgentSetting

_API_KEY_SECRET_KIND = "AI_PROVIDER_API_KEY"


@dataclass(frozen=True, slots=True)
class AIAgentSettingView:
    enabled: bool
    provider_kind: AIProviderKind
    base_url: str
    api_key_configured: bool
    model: str
    request_timeout_seconds: int
    max_context_messages: int
    data_scopes: tuple[AIDataScope, ...]
    connection_status: AIConnectionStatus
    last_test_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AIAgentSettingUpdate:
    enabled: bool
    provider_kind: AIProviderKind
    base_url: str | None
    model: str
    request_timeout_seconds: int
    max_context_messages: int
    data_scopes: tuple[AIDataScope, ...]
    api_key_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class AIAgentProbeInput:
    provider_kind: AIProviderKind
    base_url: str | None
    model: str
    request_timeout_seconds: int
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class AIAgentProbeView:
    provider_kind: AIProviderKind
    model: str
    tested_at: datetime


@dataclass(frozen=True, slots=True)
class AIAgentRuntimeConfig:
    provider: OpenAICompatibleProvider
    data_scopes: tuple[AIDataScope, ...]
    max_context_messages: int


class AIAgentService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        provider_factory: AIProviderFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._provider_factory = provider_factory or AIProviderFactory()

    def ensure_default(self) -> AIAgentSettingView:
        with self._session_factory() as session:
            record = AIAgentSettingRepository(session).create_default()
            session.commit()
            return self._view(record)

    def get(self) -> AIAgentSettingView:
        with self._session_factory() as session:
            repository = AIAgentSettingRepository(session)
            record = repository.get() or repository.create_default()
            session.commit()
            return self._view(record)

    def update(
        self,
        change: AIAgentSettingUpdate,
        *,
        expected_version: int,
    ) -> AIAgentSettingView:
        normalized_url = self._normalize_base_url(change.provider_kind, change.base_url)
        normalized_model = self._normalize_model(change.model)
        self._validate_runtime(change.request_timeout_seconds, change.max_context_messages)
        data_scopes = self._normalize_scopes(change.data_scopes)
        normalized_key = self._normalize_api_key(change.api_key)
        if change.api_key_action == "SET" and normalized_key is None:
            raise self._invalid("设置 API Key 时必须提供非空值")
        if change.api_key_action != "SET" and change.api_key is not None:
            raise self._invalid("只有 SET 动作可以携带 API Key")

        with self._session_factory() as session:
            repository = AIAgentSettingRepository(session)
            record = repository.lock_current() or repository.create_default()
            if record.version != expected_version:
                raise self._version_conflict()
            old_secret_id = record.api_key_secret_id
            new_secret_id = old_secret_id
            api_key_changed = False
            if change.api_key_action == "SET":
                assert normalized_key is not None
                new_secret_id = self._secret_store.put_in_session(
                    session,
                    kind=_API_KEY_SECRET_KIND,
                    value=normalized_key.encode("utf-8"),
                )
                api_key_changed = True
            elif change.api_key_action == "CLEAR":
                new_secret_id = None
                api_key_changed = old_secret_id is not None

            connection_changed = (
                record.provider_kind != change.provider_kind.value
                or record.base_url != normalized_url
                or record.model != normalized_model
                or api_key_changed
            )
            next_status = (
                AIConnectionStatus.UNTESTED.value
                if connection_changed
                else record.connection_status
            )
            next_enabled = change.enabled
            if connection_changed:
                next_enabled = False
            if next_enabled and (
                new_secret_id is None
                or next_status != AIConnectionStatus.OK.value
                or not normalized_model
            ):
                raise ApplicationError(
                    code="AI_PROVIDER_TEST_REQUIRED",
                    status=409,
                    title="AI Provider 尚未通过测试",
                    detail="启用 AI 助手前必须先保存配置并通过 Provider 测试",
                )

            record.enabled = next_enabled
            record.provider_kind = change.provider_kind.value
            record.base_url = normalized_url
            record.api_key_secret_id = new_secret_id
            record.model = normalized_model
            record.request_timeout_seconds = change.request_timeout_seconds
            record.max_context_messages = change.max_context_messages
            record.data_scopes = [scope.value for scope in data_scopes]
            record.connection_status = next_status
            if connection_changed:
                record.last_test_at = None
            record.version += 1
            record.updated_at = datetime.now(UTC)
            session.flush()
            if old_secret_id is not None and old_secret_id != new_secret_id:
                self._secret_store.delete_in_session(session, old_secret_id)
            session.commit()
            return self._view(record)

    async def probe_temporary(self, value: AIAgentProbeInput) -> AIAgentProbeView:
        base_url = self._normalize_base_url(value.provider_kind, value.base_url)
        model = self._normalize_model(value.model)
        self._validate_runtime(value.request_timeout_seconds, 20)
        api_key = self._normalize_api_key(value.api_key)
        if api_key is None:
            with self._session_factory() as session:
                record = AIAgentSettingRepository(session).get()
                secret_id = None if record is None else record.api_key_secret_id
            if secret_id is None:
                raise self._invalid("测试 Provider 时必须提供 API Key 或先保存 API Key")
            api_key = self._load_api_key(secret_id)
        await self._probe(
            provider_kind=value.provider_kind,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=value.request_timeout_seconds,
        )
        return AIAgentProbeView(value.provider_kind, model, datetime.now(UTC))

    async def probe_saved(self) -> AIAgentSettingView:
        with self._session_factory() as session:
            record = AIAgentSettingRepository(session).get()
            if record is None:
                raise self._invalid("尚未保存 AI Provider 配置")
            snapshot = (
                record.version,
                AIProviderKind(record.provider_kind),
                record.base_url,
                record.api_key_secret_id,
                record.model,
                record.request_timeout_seconds,
            )
        version, provider_kind, base_url, secret_id, model, timeout_seconds = snapshot
        if secret_id is None or not model:
            raise self._invalid("AI Provider API Key 与 Model 必须先保存")
        api_key = self._load_api_key(secret_id)
        tested_at = datetime.now(UTC)
        try:
            await self._probe(
                provider_kind=provider_kind,
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        except ApplicationError:
            self._persist_probe_status(version, AIConnectionStatus.FAILED, tested_at)
            raise
        self._persist_probe_status(version, AIConnectionStatus.OK, tested_at)
        return self.get()

    def runtime_config(self) -> AIAgentRuntimeConfig:
        with self._session_factory() as session:
            record = AIAgentSettingRepository(session).get()
            if record is None:
                raise self._runtime_unavailable("尚未保存 AI 助手配置")
            if not record.enabled:
                raise self._runtime_unavailable("AI 助手当前未启用")
            if record.connection_status != AIConnectionStatus.OK.value:
                raise self._runtime_unavailable("AI Provider 尚未通过连接测试")
            secret_id = record.api_key_secret_id
            if secret_id is None or not record.model:
                raise self._runtime_unavailable("AI Provider API Key 或 Model 未配置")
            provider_kind = AIProviderKind(record.provider_kind)
            base_url = record.base_url
            model = record.model
            timeout_seconds = record.request_timeout_seconds
            data_scopes = tuple(AIDataScope(scope) for scope in record.data_scopes)
            max_context_messages = record.max_context_messages
        api_key = self._load_api_key(secret_id)
        return AIAgentRuntimeConfig(
            provider=self._provider_factory.create(
                provider_kind=provider_kind,
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=float(timeout_seconds),
            ),
            data_scopes=data_scopes,
            max_context_messages=max_context_messages,
        )

    def _persist_probe_status(
        self,
        expected_version: int,
        status: AIConnectionStatus,
        tested_at: datetime,
    ) -> None:
        with self._session_factory() as session:
            updated = AIAgentSettingRepository(session).update_probe_status(
                expected_version=expected_version,
                status=status.value,
                last_test_at=tested_at,
            )
            if not updated:
                session.rollback()
                raise self._version_conflict()
            session.commit()

    async def _probe(
        self,
        *,
        provider_kind: AIProviderKind,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
    ) -> AIProviderProbeResult:
        provider = self._provider_factory.create(
            provider_kind=provider_kind,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=float(timeout_seconds),
        )
        try:
            return await provider.probe()
        except AIProviderError as exc:
            status = 502 if exc.retryable else 422
            if exc.code == "AI_PROVIDER_AUTH_FAILED":
                status = 401
            raise ApplicationError(
                code=exc.code,
                status=status,
                title="AI Provider 连接测试失败",
                detail="Provider 拒绝、不可用或返回了无法验证的响应",
            ) from exc

    def _load_api_key(self, secret_id: str) -> str:
        try:
            return self._secret_store.get(secret_id).decode("utf-8")
        except (SecretNotFound, UnicodeDecodeError) as exc:
            raise ApplicationError(
                code="AI_PROVIDER_SECRET_UNAVAILABLE",
                status=500,
                title="AI Provider 凭证不可用",
                detail="已保存的 API Key 无法读取",
            ) from exc

    @staticmethod
    def _normalize_base_url(kind: AIProviderKind, value: str | None) -> str:
        try:
            return normalize_ai_base_url(kind, value)
        except ValueError as exc:
            raise AIAgentService._invalid(str(exc)) from exc

    @staticmethod
    def _normalize_model(value: str) -> str:
        try:
            return normalize_ai_model(value)
        except ValueError as exc:
            raise AIAgentService._invalid(str(exc)) from exc

    @staticmethod
    def _normalize_api_key(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 8192:
            raise AIAgentService._invalid("AI Provider API Key 长度无效")
        if any(char in normalized for char in ("\r", "\n", "\x00")):
            raise AIAgentService._invalid("AI Provider API Key 包含无效控制字符")
        return normalized

    @staticmethod
    def _normalize_scopes(value: tuple[AIDataScope, ...]) -> tuple[AIDataScope, ...]:
        if not value:
            raise AIAgentService._invalid("AI 至少需要一个允许读取的数据范围")
        return tuple(dict.fromkeys(value))

    @staticmethod
    def _validate_runtime(timeout_seconds: int, max_context_messages: int) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise AIAgentService._invalid("AI 请求超时必须在 1～120 秒之间")
        if not 2 <= max_context_messages <= 100:
            raise AIAgentService._invalid("AI 最大上下文消息数必须在 2～100 之间")

    @staticmethod
    def _view(record: AIAgentSetting) -> AIAgentSettingView:
        return AIAgentSettingView(
            enabled=record.enabled,
            provider_kind=AIProviderKind(record.provider_kind),
            base_url=record.base_url,
            api_key_configured=record.api_key_secret_id is not None,
            model=record.model,
            request_timeout_seconds=record.request_timeout_seconds,
            max_context_messages=record.max_context_messages,
            data_scopes=tuple(AIDataScope(scope) for scope in record.data_scopes),
            connection_status=AIConnectionStatus(record.connection_status),
            last_test_at=record.last_test_at,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="AI_AGENT_CONFIG_INVALID",
            status=422,
            title="AI 助手配置无效",
            detail=detail,
        )

    @staticmethod
    def _version_conflict() -> ApplicationError:
        return ApplicationError(
            code="AI_AGENT_VERSION_CONFLICT",
            status=409,
            title="AI 助手配置已变化",
            detail="请刷新配置后重试",
        )

    @staticmethod
    def _runtime_unavailable(detail: str) -> ApplicationError:
        return ApplicationError(
            code="AI_AGENT_UNAVAILABLE",
            status=409,
            title="AI 助手当前不可用",
            detail=detail,
        )
