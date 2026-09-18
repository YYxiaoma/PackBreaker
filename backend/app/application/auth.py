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
from backend.app.infrastructure.persistence.notification_repositories import (
    AdminNotificationRepository,
)
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
    username: str
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class AdminBootstrapResult:
    created: bool
    username: str
    temporary_password: str | None = None


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    session_id: str
    expires_at: datetime
    username: str
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class AuthStatus:
    configured: bool
    authenticated: bool
    permissions: tuple[str, ...]
    expires_at: datetime | None = None
    username: str | None = None
    must_change_password: bool = False


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
        self._validate_password(password)
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

    def bootstrap_initial_admin(
        self,
        *,
        username: str,
        password: str | None,
    ) -> AdminBootstrapResult:
        normalized_username = self._normalize_username(username)
        if password is not None:
            self._validate_password(password)
        with self._session_factory() as session:
            repository = AdministratorRepository(session)
            existing = repository.get()
            if existing is not None:
                return AdminBootstrapResult(False, existing.username)
            temporary_password = None if password is not None else secrets.token_urlsafe(24)
            bootstrap_password = password or temporary_password
            assert bootstrap_password is not None
            administrator, created = repository.create(
                self._passwords.hash(bootstrap_password),
                username=normalized_username,
                must_change_password=temporary_password is not None,
            )
            if not created:
                session.rollback()
                return AdminBootstrapResult(False, administrator.username)
            session.commit()
            return AdminBootstrapResult(True, administrator.username, temporary_password)

    def login(self, *, username: str, password: str, source: str) -> AuthSessionResult:
        self._rate_limiter.check(source)
        normalized_username = username.strip()
        with self._session_factory() as session:
            repository = AdministratorRepository(session)
            if repository.get() is None:
                raise AuthError(
                    code="AUTH_SETUP_REQUIRED",
                    status=409,
                    title="尚未初始化管理员",
                    detail="请先完成管理员首次初始化",
                )
            administrator = repository.get_by_username(normalized_username)
            if administrator is None:
                self._rate_limiter.record_failure(source)
                raise self._invalid_credentials()
            verified, rehashed = self._passwords.verify(administrator.password_hash, password)
            if not verified:
                self._rate_limiter.record_failure(source)
                raise self._invalid_credentials()

            token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            logged_in_at = datetime.now(UTC)
            expires_at = logged_in_at + _SESSION_TTL
            if rehashed is not None:
                repository.update_password_hash(rehashed)
            repository.mark_login(logged_in_at=logged_in_at)
            AdminSessionRepository(session).create(
                token_digest=token_digest(token),
                csrf_digest=token_digest(csrf_token),
                expires_at=expires_at,
            )
            AdminNotificationRepository(session).create(
                event_type="AUTH_LOGIN_SUCCESS",
                title="管理员登录",
                message=(
                    f"管理员 {administrator.username} 登录成功；"
                    f"客户端标识 {token_digest(source)[:12]}。"
                ),
                severity="INFO",
            )
            session.commit()
        self._rate_limiter.clear_success(source)
        return AuthSessionResult(
            token=token,
            csrf_token=csrf_token,
            expires_at=expires_at,
            username=administrator.username,
            must_change_password=administrator.must_change_password,
        )

    def me(self, token: str | None) -> AuthStatus:
        with self._session_factory() as session:
            administrator = AdministratorRepository(session).get()
            configured = administrator is not None
            if token is None:
                return AuthStatus(configured, False, ())
            stored = AdminSessionRepository(session).find_active(
                token_digest(token), datetime.now(UTC)
            )
            if stored is None or administrator is None:
                return AuthStatus(configured, False, ())
            permissions = ("password:change",) if administrator.must_change_password else ("admin",)
            return AuthStatus(
                configured,
                True,
                permissions,
                stored.expires_at,
                administrator.username,
                administrator.must_change_password,
            )

    def require_session(
        self,
        token: str | None,
        *,
        allow_password_change_required: bool = False,
    ) -> AuthIdentity:
        if token is None:
            raise self._auth_required()
        identity = self._identity(token)
        if identity is None:
            raise self._auth_required()
        if identity.must_change_password and not allow_password_change_required:
            raise self._password_change_required()
        return identity

    def require_csrf(
        self,
        *,
        token: str | None,
        csrf_cookie: str | None,
        csrf_header: str | None,
        allow_password_change_required: bool = False,
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
            administrator = AdministratorRepository(session).get()
            if administrator is None:
                raise self._auth_required()
            identity = AuthIdentity(
                session_id=stored.id,
                expires_at=stored.expires_at,
                username=administrator.username,
                must_change_password=administrator.must_change_password,
            )
            if identity.must_change_password and not allow_password_change_required:
                raise self._password_change_required()
            return identity

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
            allow_password_change_required=True,
        )
        with self._session_factory() as session:
            AdminSessionRepository(session).revoke(identity.session_id, datetime.now(UTC))
            session.commit()

    def change_password(
        self,
        *,
        token: str | None,
        csrf_cookie: str | None,
        csrf_header: str | None,
        current_password: str,
        new_password: str,
        confirmation: str,
    ) -> None:
        identity = self.require_csrf(
            token=token,
            csrf_cookie=csrf_cookie,
            csrf_header=csrf_header,
            allow_password_change_required=True,
        )
        self._validate_password(new_password)
        if new_password != confirmation:
            raise AuthError(
                code="AUTH_PASSWORD_CONFIRMATION_MISMATCH",
                status=422,
                title="两次新密码不一致",
                detail="确认密码必须与新密码完全一致",
            )
        with self._session_factory() as session:
            administrator_repository = AdministratorRepository(session)
            administrator = administrator_repository.get()
            if administrator is None or administrator.username != identity.username:
                raise self._auth_required()
            verified, _ = self._passwords.verify(administrator.password_hash, current_password)
            if not verified:
                raise AuthError(
                    code="AUTH_CURRENT_PASSWORD_INVALID",
                    status=401,
                    title="当前密码无效",
                    detail="当前管理员密码校验失败",
                )
            same_password, _ = self._passwords.verify(administrator.password_hash, new_password)
            if same_password:
                raise AuthError(
                    code="AUTH_PASSWORD_UNCHANGED",
                    status=422,
                    title="新密码不能与当前密码相同",
                    detail="请设置一个不同的新密码",
                )
            changed_at = datetime.now(UTC)
            administrator_repository.change_password(
                self._passwords.hash(new_password), changed_at=changed_at
            )
            AdminSessionRepository(session).revoke_all(changed_at)
            AdminNotificationRepository(session).create(
                event_type="AUTH_PASSWORD_CHANGED",
                title="管理员密码已修改",
                message=f"管理员 {administrator.username} 已修改登录密码，所有管理会话已撤销。",
                severity="INFO",
            )
            session.commit()

    def _identity(self, token: str) -> AuthIdentity | None:
        with self._session_factory() as session:
            stored = AdminSessionRepository(session).find_active(
                token_digest(token), datetime.now(UTC)
            )
            if stored is None:
                return None
            administrator = AdministratorRepository(session).get()
            if administrator is None:
                return None
            return AuthIdentity(
                session_id=stored.id,
                expires_at=stored.expires_at,
                username=administrator.username,
                must_change_password=administrator.must_change_password,
            )

    @staticmethod
    def _normalize_username(value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 80
            or any(char in normalized for char in ("\r", "\n", "\x00"))
        ):
            raise AuthError(
                code="AUTH_USERNAME_INVALID",
                status=422,
                title="管理员用户名无效",
                detail="管理员用户名不能为空且最长 80 个字符",
            )
        return normalized

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 12 <= len(password) <= 256:
            raise AuthError(
                code="AUTH_PASSWORD_INVALID",
                status=422,
                title="管理员密码无效",
                detail="管理员密码长度必须在 12 到 256 个字符之间",
            )

    @staticmethod
    def _invalid_credentials() -> AuthError:
        return AuthError(
            code="AUTH_INVALID_CREDENTIALS",
            status=401,
            title="认证失败",
            detail="管理员用户名或密码无效",
        )

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
    def _password_change_required() -> AuthError:
        return AuthError(
            code="AUTH_PASSWORD_CHANGE_REQUIRED",
            status=403,
            title="必须先修改临时密码",
            detail="当前账户使用一次性临时密码，只允许进入修改密码流程",
        )

    @staticmethod
    def _csrf_invalid() -> AuthError:
        return AuthError(
            code="CSRF_INVALID",
            status=403,
            title="CSRF 校验失败",
            detail="CSRF Cookie 与请求头不匹配或已失效",
        )
