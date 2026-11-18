from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.app.infrastructure.transient_updater import TransientUpdaterLauncher
from backend.app.infrastructure.updater_protocol import UPDATER_PROTOCOL_VERSION, UpdaterStatus


def _succeeded_status() -> UpdaterStatus:
    return UpdaterStatus(
        protocol_version=UPDATER_PROTOCOL_VERSION,
        helper_version="1.1.8",
        phase="succeeded",
        message="PackBreaker 已升级到 1.1.8",
        request_id="pb-upgrade-test",
        current_version="1.1.7",
        target_version="1.1.8",
    )


def test_state_file_is_readable_by_the_main_application(tmp_path: Path) -> None:
    """一次性 helper 以 root 写状态时必须留下可读副本。

    v1.1.7 升级到 v1.1.8 之后，state.json 是 root:root 0600，以 PUID 运行的主应用
    读不到，下一次升级就被误判成「上一次升级现场需要人工核对」而永久置灰。
    """

    launcher = TransientUpdaterLauncher(config_dir=tmp_path)
    launcher._write_status(_succeeded_status())

    state = tmp_path / "transient-updater" / "state.json"
    assert state.is_file()
    assert state.stat().st_mode & 0o777 == 0o644


def test_unreadable_state_is_quarantined_and_reported_as_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root 不受文件权限限制，无法构造不可读状态")

    launcher = TransientUpdaterLauncher(config_dir=tmp_path)
    monkeypatch.setattr(launcher, "_require_docker_access", lambda: None)

    state = tmp_path / "transient-updater" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"phase":"succeeded"}', encoding="utf-8")
    state.chmod(0o000)

    status = launcher.status()

    assert status.phase == "idle"
    assert not state.exists()
    assert list(state.parent.glob("state.unreadable-*.json"))
