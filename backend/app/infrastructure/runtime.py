import errno
import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
    sqlite_database_url,
)
from backend.app.infrastructure.security import MasterKeyFile, SecretCipher


class InstanceLockUnavailable(RuntimeError):
    """同一 config 目录已经有活动执行实例。"""


class InstanceLock:
    """Linux 单实例执行锁；锁文件本身长期保留，生命周期由 flock 控制。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self.held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("实例锁路径不是普通文件")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise InstanceLockUnavailable("已有 PackBreaker 实例持有执行锁") from exc
                raise
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    database: str
    migrations: str
    secrets: str
    worker_slot: str
    current_revision: str | None = None
    expected_revision: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": {
                "database": self.database,
                "migrations": self.migrations,
                "secrets": self.secrets,
                "worker_slot": self.worker_slot,
            },
            "migration": {
                "current_revision": self.current_revision,
                "expected_revision": self.expected_revision,
            },
        }


def ensure_config_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISDIR(mode):
        raise RuntimeError("配置目录必须是真实目录，不能是符号链接")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError("配置目录权限不足")


def make_migration_config(database_url: str) -> Config:
    migrations = Path(__file__).resolve().parents[2] / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("prepend_sys_path", ".")
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


def migrate_database(database_url: str) -> None:
    command.upgrade(make_migration_config(database_url), "head")


def migration_revisions(engine: Engine) -> tuple[str | None, str | None]:
    config = make_migration_config(engine.url.render_as_string(hide_password=False))
    expected = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return current, expected


class RuntimeManager:
    """持有进程级锁和数据库生命周期；真实 worker 接入前只声明执行槽就绪。"""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.instance_lock = InstanceLock(settings.instance_lock_path)
        self.engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._secret_cipher: SecretCipher | None = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            raise RuntimeError("运行时尚未建立数据库会话工厂")
        return self._session_factory

    @property
    def secret_cipher(self) -> SecretCipher:
        if self._secret_cipher is None:
            raise RuntimeError("运行时尚未加载 secret 主密钥")
        return self._secret_cipher

    def start(self) -> None:
        if self._started:
            return
        ensure_config_directory(self.settings.config_dir)
        self.instance_lock.acquire()
        try:
            master_key = MasterKeyFile.load_or_create(self.settings.resolved_secret_key_file)
            self._secret_cipher = SecretCipher(master_key)
            if not self._secret_cipher.self_test():
                raise RuntimeError("secret 加解密自检失败")
            database_url = sqlite_database_url(self.settings.database_path)
            migrate_database(database_url)
            self.engine = create_sqlite_engine(self.settings.database_path)
            self._session_factory = create_session_factory(self.engine)
            self._started = True
            report = self.readiness()
            if not report.ready:
                raise RuntimeError("本地运行时就绪检查未通过")
        except BaseException:
            self._cleanup()
            raise

    def stop(self) -> None:
        self._cleanup()

    def readiness(self) -> ReadinessReport:
        if not self._started or self.engine is None:
            return ReadinessReport(
                False,
                "not_started",
                "not_started",
                "not_started",
                "not_started",
            )

        database_status = "ok"
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
        except SQLAlchemyError:
            database_status = "unavailable"

        current_revision: str | None = None
        expected_revision: str | None = None
        migration_status = "unknown"
        if database_status == "ok":
            try:
                current_revision, expected_revision = migration_revisions(self.engine)
                migration_status = "ok" if current_revision == expected_revision else "outdated"
            except Exception:
                # 就绪端点不能把迁移脚本/元数据异常升级成未处理的 HTTP 500。
                migration_status = "unavailable"

        secret_status = "unavailable"
        if self._secret_cipher is not None:
            try:
                secret_status = "ok" if self._secret_cipher.self_test() else "unavailable"
            except Exception:
                secret_status = "unavailable"

        worker_slot = "ok" if self.instance_lock.held else "unavailable"
        ready = all(
            status == "ok"
            for status in (database_status, migration_status, secret_status, worker_slot)
        )
        return ReadinessReport(
            ready,
            database_status,
            migration_status,
            secret_status,
            worker_slot,
            current_revision,
            expected_revision,
        )

    def _cleanup(self) -> None:
        engine = self.engine
        self.engine = None
        self._session_factory = None
        self._secret_cipher = None
        self._started = False
        if engine is not None:
            engine.dispose()
        self.instance_lock.release()
