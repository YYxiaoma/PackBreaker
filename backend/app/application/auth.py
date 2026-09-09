import hmac
import math
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.infrastructure.persistence.security_repositories import (
    AdministratorRepository,
    AdminSessionRepository,
)
from backend.app.infrastructure.security import PasswordService, token_digest

_SESSION_TTL = timedelta(hours=12)
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_FAILURE_LIMIT = 5


@dataclass(frozen=True, slots=True)
class AuthSessionResult:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    session_id: str
    expires_at: datetime


class AuthError(ApplicationError):
    pass


class LoginRateLimiter:
    """进程内双维度登录限速；来源只以摘要作为 bucket key。"""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, source: str) -> None:
        now = time.monotonic()
        with self._lock:
            retry_after = max(
                self._retry_after_locked(self._source_key(source), now),
                self._retry_after_locked("account:admin", now),
            )
        if retry_after > 0:
            raise AuthError(
                code="AUTH_RATE_LIMITED",
                status=429,
                title="登录尝试过多",
                detail="登录失败次数过多，请稍后重试",
                retry_after=retry_after,
            )

    def record_failure(self, source: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in (self._source_key(source), "account:admin"):
                self._prune_locked(key, now)
                self._failures[key].append(now)

    def clear_success(self, source: str) -> None:
        with self._lock:
            self._failures.pop(self._source_key(source), None)
            self._failures.pop("account:admin", None)

    @staticmethod
    def _source_key(source: str) -> str:
        return f"source:{token_digest(source)[:32]}"

    def _retry_after_locked(self, key: str, now: float) -> int:
        self._prune_locked(key, now)
        failures = self._failures[key]
        if len(failures) < _LOGIN_FAILURE_LIMIT:
            return 0
        return max(1, math.ceil(_LOGIN_WINDOW_SECONDS - (now - failures[0])))

    def _prune_locked(self, key: str, now: float) -> None:
        failures = self._failures[key]
        threshold = now - _LOGIN_WINDOW_SECONDS
        while failures and failures[0] <= threshold:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)


class AuthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        password_service: PasswordService | None = None,
        rate_limiter: LoginRateLimiter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._passwords = password_service or PasswordService()
        self._rate_limiter = rate_limiter or LoginRateLimiter()

    def setup(self, password: str) -> None:
        with self._session_factory() as session:
            repository = AdministratorRepository(session)
            if repository.get() is not None:
                raise self._setup_complete()
            password_hash = self._passwords.hash(password)
            _, created = repository.create(password_hash)
            if not created:
                session.rollback()
                raise self._setup_complete()
            session.commit()

    def login(self, *, password: str, source: str) -> AuthSessionResult:
        self._rate_limiter.check(source)
        with self._session_factory() as session:
            administrator = AdministratorRepository(session).get()
            if administrator is None:
                raise AuthError(
                    code="AUTH_SETUP_REQUIRED",
                    status=409,
                    title="尚未初始化管理员",
                    detail="请先完成管理员首次初始化",
                )
            verified, rehashed = self._passwords.verify(administrator.password_hash, password)
            if not verified:
                self._rate_limiter.record_failure(source)
                raise AuthError(
                    code="AUTH_INVALID_CREDENTIALS",
                    status=401,
                    title="认证失败",
                    detail="管理员口令无效",
                )

            token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            expires_at = datetime.now(UTC) + _SESSION_TTL
            if rehashed is not None:
                AdministratorRepository(session).update_password_hash(rehashed)
            AdminSessionRepository(session).create(
                token_digest=token_digest(token),
                csrf_digest=token_digest(csrf_token),
                expires_at=expires_at,
            )
            session.commit()
        self._rate_limiter.clear_success(source)
        return AuthSessionResult(token=token, csrf_token=csrf_token, expires_at=expires_at)

    def me(self, token: str | None) -> dict[str, object]:
        if token is None:
            return {"authenticated": False, "permissions": []}
        identity = self._identity(token)
        if identity is None:
            return {"authenticated": False, "permissions": []}
        return {
            "authenticated": True,
            "permissions": ["admin"],
            "expires_at": identity.expires_at.isoformat().replace("+00:00", "Z"),
        }

    def require_session(self, token: str | None) -> AuthIdentity:
        if token is None:
            raise self._auth_required()
        identity = self._identity(token)
        if identity is None:
            raise self._auth_required()
        return identity

    def require_csrf(
        self,
        *,
        token: str | None,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> AuthIdentity:
        if token is None:
            raise self._auth_required()
        with self._session_factory() as session:
            stored = AdminSessionRepository(session).find_active(
                token_digest(token),
                datetime.now(UTC),
            )
            if stored is None:
                raise self._auth_required()
            if csrf_cookie is None or csrf_header is None:
                raise self._csrf_invalid()
            if not hmac.compare_digest(csrf_cookie, csrf_header):
                raise self._csrf_invalid()
            if not hmac.compare_digest(stored.csrf_digest, token_digest(csrf_cookie)):
                raise self._csrf_invalid()
            return AuthIdentity(session_id=stored.id, expires_at=stored.expires_at)

    def logout(
        self,
        *,
        token: str | None,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> None:
        identity = self.require_csrf(
            token=token,
            csrf_cookie=csrf_cookie,
            csrf_header=csrf_header,
        )
        with self._session_factory() as session:
            AdminSessionRepository(session).revoke(identity.session_id, datetime.now(UTC))
            session.commit()

    def _identity(self, token: str) -> AuthIdentity | None:
        with self._session_factory() as session:
            stored = AdminSessionRepository(session).find_active(
                token_digest(token), datetime.now(UTC)
            )
            if stored is None:
                return None
            return AuthIdentity(session_id=stored.id, expires_at=stored.expires_at)

    @staticmethod
    def _setup_complete() -> AuthError:
        return AuthError(
            code="AUTH_SETUP_COMPLETE",
            status=409,
            title="管理员已初始化",
            detail="管理员首次初始化已经完成",
        )

    @staticmethod
    def _auth_required() -> AuthError:
        return AuthError(
            code="AUTH_REQUIRED",
            status=401,
            title="需要认证",
            detail="管理员会话不存在、已过期或已撤销",
        )

    @staticmethod
    def _csrf_invalid() -> AuthError:
        return AuthError(
            code="CSRF_INVALID",
            status=403,
            title="CSRF 校验失败",
            detail="CSRF Cookie 与请求头不匹配或已失效",
        )
