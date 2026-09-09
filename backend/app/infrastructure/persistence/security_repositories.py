from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.infrastructure.persistence.models import (
    Administrator,
    AdminSession,
    ApiToken,
    SecretRecord,
    new_uuid,
    utc_now,
)


class AdministratorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> Administrator | None:
        return self._session.get(Administrator, "admin")

    def create(self, password_hash: str) -> tuple[Administrator, bool]:
        existing = self.get()
        if existing is not None:
            return existing, False
        now = utc_now()
        administrator = Administrator(
            id="admin",
            password_hash=password_hash,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(administrator)
                self._session.flush()
        except IntegrityError:
            concurrent = self.get()
            if concurrent is None:
                raise
            return concurrent, False
        return administrator, True

    def update_password_hash(self, password_hash: str) -> None:
        self._session.execute(
            update(Administrator)
            .where(Administrator.id == "admin")
            .values(password_hash=password_hash, updated_at=utc_now())
        )


class AdminSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        expires_at: datetime,
    ) -> AdminSession:
        session = AdminSession(
            id=new_uuid(),
            administrator_id="admin",
            token_digest=token_digest,
            csrf_digest=csrf_digest,
            expires_at=expires_at,
            revoked_at=None,
            created_at=utc_now(),
        )
        self._session.add(session)
        self._session.flush()
        return session

    def find_active(self, token_digest: str, now: datetime) -> AdminSession | None:
        return self._session.scalar(
            select(AdminSession).where(
                AdminSession.token_digest == token_digest,
                AdminSession.revoked_at.is_(None),
                AdminSession.expires_at > now,
            )
        )

    def revoke(self, session_id: str, now: datetime) -> None:
        self._session.execute(
            update(AdminSession)
            .where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )


class ApiTokenRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        name: str,
        token_digest: str,
        scopes: list[str],
        expires_at: datetime,
    ) -> ApiToken:
        token = ApiToken(
            id=new_uuid(),
            name=name,
            token_digest=token_digest,
            scopes=list(scopes),
            expires_at=expires_at,
            revoked_at=None,
            created_at=utc_now(),
        )
        self._session.add(token)
        self._session.flush()
        return token

    def list_all(self) -> list[ApiToken]:
        return list(self._session.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())))

    def get(self, token_id: str) -> ApiToken | None:
        return self._session.get(ApiToken, token_id)

    def find_active(self, digest: str, now: datetime) -> ApiToken | None:
        return self._session.scalar(
            select(ApiToken).where(
                ApiToken.token_digest == digest,
                ApiToken.revoked_at.is_(None),
                ApiToken.expires_at > now,
            )
        )

    def revoke(self, token_id: str, now: datetime) -> None:
        self._session.execute(
            update(ApiToken)
            .where(ApiToken.id == token_id, ApiToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )


class SecretRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        secret_id: str,
        kind: str,
        ciphertext: str,
        key_version: int,
    ) -> SecretRecord:
        now = utc_now()
        record = SecretRecord(
            id=secret_id,
            kind=kind,
            ciphertext=ciphertext,
            key_version=key_version,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get(self, secret_id: str) -> SecretRecord | None:
        return self._session.get(SecretRecord, secret_id)

    def delete(self, secret_id: str) -> None:
        self._session.execute(delete(SecretRecord).where(SecretRecord.id == secret_id))
