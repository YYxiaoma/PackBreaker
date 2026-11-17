from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _empty_official_templates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API 测试不安装镜像内置模板，保持模板列表断言与数据目录完全封闭。"""

    empty = tmp_path / "official-templates-empty"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PACKBREAKER_OFFICIAL_TEMPLATE_DIR", str(empty))
