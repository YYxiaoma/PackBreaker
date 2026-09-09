import base64
import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MASTER_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretKeyError(RuntimeError):
    """主密钥文件不满足安全要求。"""


class SecretDecryptionError(RuntimeError):
    """密文无法由当前主密钥和绑定上下文解密。"""


class MasterKeyFile:
    """安全创建并读取 0600 的 256-bit 主密钥文件。"""

    @staticmethod
    def load_or_create(path: Path) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(FileExistsError):
            MasterKeyFile._create(path)
        return MasterKeyFile._read(path)

    @staticmethod
    def _create(path: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            key = os.urandom(_MASTER_KEY_BYTES)
            written = os.write(fd, key)
            if written != len(key):
                raise SecretKeyError("主密钥文件写入不完整")
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _read(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise SecretKeyError("无法安全打开主密钥文件") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretKeyError("主密钥路径必须是普通文件")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise SecretKeyError("主密钥文件权限过宽，必须为 0600")
            key = os.read(fd, _MASTER_KEY_BYTES + 1)
            if len(key) != _MASTER_KEY_BYTES:
                raise SecretKeyError("主密钥文件长度无效")
            return key
        finally:
            os.close(fd)


class PasswordService:
    """使用 argon2-cffi 默认 Argon2id 参数保存管理员口令。"""

    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> tuple[bool, str | None]:
        try:
            verified = self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, None
        if not verified:
            return False, None
        rehashed = self.hash(password) if self._hasher.check_needs_rehash(password_hash) else None
        return True, rehashed


class SecretCipher:
    """AES-256-GCM 密文；AAD 绑定 secret 身份、类型和密钥版本。"""

    def __init__(self, key: bytes) -> None:
        if len(key) != _MASTER_KEY_BYTES:
            raise ValueError("SecretCipher 需要 32 字节主密钥")
        self._cipher = AESGCM(key)

    def encrypt(self, *, secret_id: str, kind: str, key_version: int, plaintext: bytes) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, self._aad(secret_id, kind, key_version))
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, *, secret_id: str, kind: str, key_version: int, ciphertext: str) -> bytes:
        try:
            payload = base64.b64decode(ciphertext.encode("ascii"), altchars=b"-_", validate=True)
            if len(payload) <= _NONCE_BYTES:
                raise ValueError("密文长度无效")
            nonce = payload[:_NONCE_BYTES]
            encrypted = payload[_NONCE_BYTES:]
            return self._cipher.decrypt(
                nonce,
                encrypted,
                self._aad(secret_id, kind, key_version),
            )
        except (InvalidTag, ValueError, UnicodeError) as exc:
            raise SecretDecryptionError("secret 密文认证失败") from exc

    def self_test(self) -> bool:
        marker = os.urandom(32)
        encrypted = self.encrypt(
            secret_id="runtime-self-test",
            kind="SELF_TEST",
            key_version=1,
            plaintext=marker,
        )
        return (
            self.decrypt(
                secret_id="runtime-self-test",
                kind="SELF_TEST",
                key_version=1,
                ciphertext=encrypted,
            )
            == marker
        )

    @staticmethod
    def _aad(secret_id: str, kind: str, key_version: int) -> bytes:
        return f"packbreaker-secret-v1\0{secret_id}\0{kind}\0{key_version}".encode()


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
