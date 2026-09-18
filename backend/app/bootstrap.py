from __future__ import annotations

import sys
from typing import TextIO


def emit_temporary_admin_password(
    *,
    username: str,
    password: str,
    stream: TextIO | None = None,
) -> None:
    """只向进程标准错误输出一次性管理员凭证，故意绕过 logging sink。"""

    target = stream or sys.stderr
    target.write(
        "\n"
        "============================================================\n"
        "PackBreaker one-time administrator credential\n"
        f"Username: {username}\n"
        f"Temporary password: {password}\n"
        "Change this password immediately after signing in.\n"
        "============================================================\n"
    )
    target.flush()
