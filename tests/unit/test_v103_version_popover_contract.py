"""The v1.0.3 version popover hides manual rollback but retains safe auto rollback."""

from __future__ import annotations

from pathlib import Path

COMPONENT = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "VersionPopover.vue"
)


def test_popover_removes_manual_rollback_without_disabling_safe_upgrade() -> None:
    source = COMPONENT.read_text(encoding="utf-8")
    assert "版本回退" not in source
    assert "rollbackOpen" not in source
    assert "rollback-section" not in source
    assert "version-action-row" not in source
    assert "立即升级到 v" in source
    assert "健康失败自动回滚" in source
    assert "ElMessageBox.confirm" in source
    assert "pendingIdempotencyKey" in source
    assert "UPDATER_DOCKER_SOCKET_PERMISSION_DENIED" in source
    assert "UPDATER_TARGET_CONTAINER_UNAVAILABLE" in source
