from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.application.ai_tools import AIToolService
from backend.app.application.errors import ApplicationError
from backend.app.config import AppSettings
from backend.app.domain.ai_agent import AIDataScope, AIToolName
from backend.app.main import create_app


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        task_driver_interval_seconds=3600,
        task_definition_driver_interval_seconds=3600,
        notification_driver_interval_seconds=3600,
        backup_driver_interval_seconds=3600,
    )


@pytest.mark.asyncio
async def test_ai_tools_enforce_scope_and_do_not_expose_configuration_paths(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app):
        service: AIToolService = app.state.ai_tool_service
        with pytest.raises(ApplicationError) as denied:
            await service.execute(
                AIToolName.LIST_TASK_DEFINITIONS,
                {},
                allowed_scopes=(AIDataScope.SYSTEM_HEALTH,),
            )
        assert denied.value.code == "AI_TOOL_SCOPE_DENIED"

        result = await service.execute(
            AIToolName.LIST_TASK_DEFINITIONS,
            {},
            allowed_scopes=(AIDataScope.TASK_EXECUTIONS,),
        )
        serialized = str(result.payload)
        assert result.payload == {"items": [], "count": 0}
        assert "output_directory" not in serialized
        assert "source_directory" not in serialized
        assert "config_snapshot" not in serialized


@pytest.mark.asyncio
async def test_ai_health_and_help_tools_are_bounded_read_only(tmp_path: Path) -> None:
    root = tmp_path / "help-root"
    root.mkdir()
    (root / "README.md").write_text("PackBreaker\n安全原则：只读工具。\n", encoding="utf-8")
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app):
        original: AIToolService = app.state.ai_tool_service
        service = AIToolService(
            app.state.runtime.session_factory,
            settings=app.state.runtime.settings,
            runtime=app.state.runtime,
            site_reliability_registry=app.state.site_reliability_registry,
            task_definition_service=app.state.task_definition_service,
            downloader_service=app.state.downloader_service,
            site_service=app.state.site_service,
            system_upgrade_service=app.state.system_upgrade_service,
            task_driver=app.state.task_driver,
            notification_driver=app.state.notification_driver,
            backup_driver=app.state.backup_driver,
            help_root=root,
        )
        assert original.available_tools((AIDataScope.SYSTEM_HEALTH,)) == (
            AIToolName.GET_SYSTEM_HEALTH,
        )
        health = await service.execute(
            AIToolName.GET_SYSTEM_HEALTH,
            {},
            allowed_scopes=(AIDataScope.SYSTEM_HEALTH,),
        )
        assert health.payload["status"] in {"ok", "warning", "blocked"}

        help_result = await service.execute(
            AIToolName.SEARCH_HELP_DOCS,
            {"query": "只读"},
            allowed_scopes=(AIDataScope.HELP_DOCS,),
        )
        assert help_result.payload["count"] == 1
        assert help_result.payload["items"][0]["document"] == "README.md"  # type: ignore[index]


@pytest.mark.asyncio
async def test_ai_tool_rejects_undeclared_arguments(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app):
        service: AIToolService = app.state.ai_tool_service
        with pytest.raises(ApplicationError) as invalid:
            await service.execute(
                AIToolName.GET_SYSTEM_HEALTH,
                {"url": "https://example.invalid"},
                allowed_scopes=(AIDataScope.SYSTEM_HEALTH,),
            )
        assert invalid.value.code == "AI_TOOL_ARGUMENT_INVALID"
