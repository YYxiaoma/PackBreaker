import os
import stat
from pathlib import Path

import pytest

from backend.app.infrastructure.runtime import InstanceLock, InstanceLockUnavailable


def test_instance_lock_excludes_second_holder_and_can_be_reacquired(tmp_path: Path) -> None:
    path = tmp_path / "config" / "packbreaker.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    first.acquire()
    try:
        with pytest.raises(InstanceLockUnavailable):
            second.acquire()
    finally:
        first.release()

    assert path.exists()
    assert path.read_text().strip() == str(os.getpid())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    second.acquire()
    assert second.held
    second.release()
