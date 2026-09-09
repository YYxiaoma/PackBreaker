from sqlalchemy.orm import Session, sessionmaker

from backend.app.infrastructure.persistence.models import new_uuid
from backend.app.infrastructure.persistence.security_repositories import SecretRepository
from backend.app.infrastructure.security import SecretCipher


class SecretNotFound(KeyError):
    pass


class SecretStore:
    """业务层只接触明文边界；数据库 repository 永远只看到认证密文。"""

    def __init__(self, session_factory: sessionmaker[Session], cipher: SecretCipher) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    def put(self, *, kind: str, value: bytes) -> str:
        with self._session_factory() as session:
            secret_id = self.put_in_session(session, kind=kind, value=value)
            session.commit()
        return secret_id

    def put_in_session(self, session: Session, *, kind: str, value: bytes) -> str:
        if not kind or len(kind) > 64:
            raise ValueError("secret kind 长度无效")
        secret_id = new_uuid()
        key_version = 1
        ciphertext = self._cipher.encrypt(
            secret_id=secret_id,
            kind=kind,
            key_version=key_version,
            plaintext=value,
        )
        SecretRepository(session).create(
            secret_id=secret_id,
            kind=kind,
            ciphertext=ciphertext,
            key_version=key_version,
        )
        return secret_id

    def get(self, secret_id: str) -> bytes:
        with self._session_factory() as session:
            record = SecretRepository(session).get(secret_id)
            if record is None:
                raise SecretNotFound(secret_id)
            return self._cipher.decrypt(
                secret_id=record.id,
                kind=record.kind,
                key_version=record.key_version,
                ciphertext=record.ciphertext,
            )

    def delete_in_session(self, session: Session, secret_id: str) -> None:
        SecretRepository(session).delete(secret_id)
