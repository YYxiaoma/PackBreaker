from __future__ import annotations

import os
import sys

from backend.app.container_entrypoint import _numeric_id


def _drop_healthcheck_privileges() -> None:
    """Run Docker's periodic healthcheck with the configured application identity."""

    if os.geteuid() != 0:
        return
    uid = _numeric_id("PUID", 1000)
    gid = _numeric_id("PGID", 1000)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> int:
    _drop_healthcheck_privileges()
    from backend.app.healthcheck import main as healthcheck_main

    return healthcheck_main()


if __name__ == "__main__":
    sys.exit(main())
