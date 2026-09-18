from __future__ import annotations

import uvicorn

from backend.app.application.auth import AuthService
from backend.app.bootstrap import emit_temporary_admin_password
from backend.app.config import AppSettings
from backend.app.infrastructure.app_logging import configure_logging
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.main import create_app


def main() -> None:
    settings = AppSettings()
    configure_logging(
        settings.log_level,
        log_dir=settings.log_dir,
        max_bytes=settings.log_file_max_bytes,
        backup_count=settings.log_file_backup_count,
    )
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        configured_password = (
            settings.admin_password.get_secret_value()
            if settings.admin_password is not None
            else None
        )
        bootstrap = AuthService(runtime.session_factory).bootstrap_initial_admin(
            username=settings.admin_username,
            password=configured_password,
        )
        if bootstrap.temporary_password is not None:
            emit_temporary_admin_password(
                username=bootstrap.username,
                password=bootstrap.temporary_password,
            )
        uvicorn.run(
            create_app(settings=settings, runtime=runtime),
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            workers=1,
        )
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
