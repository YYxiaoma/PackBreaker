import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.auth import AuthError
from backend.app.domain.auth import ApiScope
from backend.app.infrastructure.persistence.models import ApiToken
from backend.app.infrastructure.persistence.security_repositories import ApiTokenRepository
from backend.app.infrastructure.security import token_digest

_TOKEN_PREFIX = "pbk_"


@dataclass(frozen=True, slots=True)
class ApiTokenIdentity:
    token_id: str
    scopes: frozenset[ApiScope]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ApiTokenCreated:
    id: str
    name: str
    token: str
    scopes: tuple[ApiScope, ...]
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApiTokenView:
    id: str
    name: str
    scopes: tuple[ApiScope, ...]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class ApiTokenService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        name: str,
        scopes: list[ApiScope],
        expires_at: datetime,
    ) -> ApiTokenCreated:
        normalized_expiry = self._normalize_expiry(expires_at)
        normalized_scopes = tuple(sorted(set(scopes), key=str))
        if not normalized_scopes:
            raise AuthError(
                code="API_TOKEN_SCOPES_REQUIRED",
                status=422,
                title="API Token 范围无效",
                detail="至少需要一个 API Token scope",
            )

        plaintext = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._session_factory() as session:
            record = ApiTokenRepository(session).create(
                name=name,
                token_digest=token_digest(plaintext),
                scopes=[scope.value for scope in normalized_scopes],
                expires_at=normalized_expiry,
            )
            session.commit()
            session.refresh(record)
            return ApiTokenCreated(
                id=record.id,
                name=record.name,
                token=plaintext,
                scopes=normalized_scopes,
                expires_at=record.expires_at,
                created_at=record.created_at,
            )

    def list(self) -> list[ApiTokenView]:
        with self._session_factory() as session:
            return [self._view(record) for record in ApiTokenRepository(session).list_all()]

    def revoke(self, token_id: str) -> None:
        with self._session_factory() as session:
            repository = ApiTokenRepository(session)
            existing = repository.get(token_id)
            if existing is None:
                raise AuthError(
                    code="API_TOKEN_NOT_FOUND",
                    status=404,
                    title="API Token 不存在",
                    detail="指定的 API Token 不存在",
                )
            if existing.revoked_at is None:
                repository.revoke(token_id, datetime.now(UTC))
                session.commit()

    def authenticate(self, token: str, required_scope: ApiScope) -> ApiTokenIdentity:
        if not token.startswith(_TOKEN_PREFIX):
            raise self._invalid_token()
        with self._session_factory() as session:
            stored = ApiTokenRepository(session).find_active(token_digest(token), datetime.now(UTC))
            if stored is None:
                raise self._invalid_token()
            scopes = frozenset(ApiScope(value) for value in stored.scopes)
            if required_scope not in scopes:
                raise AuthError(
                    code="API_TOKEN_SCOPE_FORBIDDEN",
                    status=403,
                    title="API Token 权限不足",
                    detail=f"当前 API Token 缺少 {required_scope.value} scope",
                )
            return ApiTokenIdentity(
                token_id=stored.id,
                scopes=scopes,
                expires_at=stored.expires_at,
            )

    @staticmethod
    def _normalize_expiry(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise AuthError(
                code="API_TOKEN_EXPIRY_INVALID",
                status=422,
                title="API Token 过期时间无效",
                detail="过期时间必须包含时区",
            )
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise AuthError(
                code="API_TOKEN_EXPIRY_INVALID",
                status=422,
                title="API Token 过期时间无效",
                detail="过期时间必须晚于当前时间",
            )
        return normalized

    @staticmethod
    def _view(record: ApiToken) -> ApiTokenView:
        return ApiTokenView(
            id=record.id,
            name=record.name,
            scopes=tuple(ApiScope(value) for value in record.scopes),
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            created_at=record.created_at,
        )

    @staticmethod
    def _invalid_token() -> AuthError:
        return AuthError(
            code="API_TOKEN_INVALID",
            status=401,
            title="API Token 无效",
            detail="API Token 不存在、已过期或已撤销",
        )
