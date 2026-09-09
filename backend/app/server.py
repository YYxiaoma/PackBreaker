from __future__ import annotations

import uvicorn

from backend.app.config import AppSettings
from backend.app.main import create_app


def main() -> None:
    settings = AppSettings()
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=False,
        server_header=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
