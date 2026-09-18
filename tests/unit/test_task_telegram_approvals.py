from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_telegram_approvals import TaskTelegramApprovalService
from backend.app.domain.task_approval import (
    TaskApprovalDecisionSource,
    TaskApprovalState,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    TaskApprovalRecord,
    TaskDefinition,
    TaskExecution,
    TaskExecutionItem,
    TaskRiskSummaryRecord,
)

_APPROVAL_ID = "11111111-1111-1111-1111-111111111111"
_PLAN_DIGEST = "a" * 64


class FakeExecutionService:
    def __init__(self, *, stale: bool = False) -> None:
        self.stale = stale
        self.calls: list[dict[str, Any]] = []

    async def decide_execution_approval(
        self,
        definition_id: str,
        execution_id: str,
        item_id: str,
        **kwargs: Any,
    ) -> object:
        self.calls.append(
            {
                "definition_id": definition_id,
                "execution_id": execution_id,
                "item_id": item_id,
                **kwargs,
            }
        )
        if self.stale:
            raise ApplicationError(
                code="TASK_APPROVAL_PLAN_STALE",
                status=409,
                title="审批计划已失效",
                detail="synthetic stale plan",
            )
        return object()


def _service(
    tmp_path: Path,
    *,
    stale: bool = False,
) -> tuple[TaskTelegramApprovalService, FakeExecutionService, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    execution_service = FakeExecutionService(stale=stale)
    service = TaskTelegramApprovalService(
        factory,
        cast(Any, execution_service),
    )
    _seed(factory)
    return service, execution_service, factory


def _seed(factory: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            TaskDefinition(
                id="definition-1",
                name="高风险任务",
                kind="MANUAL",
                status="ENABLED",
                site_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TaskExecution(
                id="execution-1",
                task_definition_id="definition-1",
                task_name="高风险任务",
                trigger="MANUAL",
                status="PENDING",
                phase="WAITING",
                source_execution_id=None,
                trace_id="00000000-0000-0000-0000-000000000001",
                config_snapshot={},
                discovered_count=1,
                success_count=0,
                failed_count=0,
                skipped_count=0,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
        )
        session.add(
            TaskExecutionItem(
                id="item-1",
                execution_id="execution-1",
                unpack_task_id="task-1",
                source_object_key="source-1",
                name="Movie.mkv",
                source="/data/Movie.mkv",
                size_bytes=123,
                phase="WAITING",
                progress=0,
                result=None,
                error_code=None,
                error_summary_zh=None,
                technical_detail=None,
                retryable=False,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TaskRiskSummaryRecord(
                id="risk-1",
                task_id="task-1",
                task_unit_id="unit-1",
                execution_plan_id="plan-1",
                plan_digest=_PLAN_DIGEST,
                risk_level="HIGH",
                reason_codes=["HIGH_RISK_ACTION:DELETE_SOURCE"],
                action_kinds=["DELETE_SOURCE"],
                hardlink_count=1,
                client_fetch_count=0,
                create_directory_count=0,
                estimated_download_bytes_upper_bound=0,
                risk_digest="b" * 64,
                created_at=now,
            )
        )
        session.add(
            TaskApprovalRecord(
                id=_APPROVAL_ID,
                task_id="task-1",
                task_unit_id="unit-1",
                execution_plan_id="plan-1",
                risk_summary_id="risk-1",
                plan_digest=_PLAN_DIGEST,
                state=TaskApprovalState.PENDING.value,
                decision_source=None,
                actor_kind=None,
                actor_id=None,
                decision_note=None,
                idempotency_key_digest=None,
                decided_at=None,
                telegram_notified_at=None,
                telegram_chat_id=None,
                telegram_message_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_pending_request_builds_digest_bound_inline_keyboard_and_notifies_once(
    tmp_path: Path,
) -> None:
    service, _execution_service, factory = _service(tmp_path)

    requests = service.list_pending_requests()

    assert len(requests) == 1
    request = requests[0]
    assert "当前未执行任何真实数据修改" in request.message_text
    keyboard = cast(list[list[dict[str, str]]], request.reply_markup["inline_keyboard"])
    callback_values = [button["callback_data"] for row in keyboard for button in row]
    assert callback_values == [
        f"pb1:A:{_APPROVAL_ID}:aaaaaaaa",
        f"pb1:R:{_APPROVAL_ID}:aaaaaaaa",
        f"pb1:V:{_APPROVAL_ID}:aaaaaaaa",
    ]
    assert all(len(value.encode()) <= 64 for value in callback_values)

    assert service.mark_notified(
        _APPROVAL_ID,
        chat_id="-100123",
        message_id="99",
    )
    assert service.list_pending_requests() == ()
    with factory() as session:
        approval = session.get(TaskApprovalRecord, _APPROVAL_ID)
        assert approval is not None
        assert approval.telegram_notified_at is not None
        assert approval.telegram_chat_id == "-100123"
        assert approval.telegram_message_id == "99"


@pytest.mark.asyncio
async def test_view_callback_is_read_only_and_approve_uses_shared_decision_service(
    tmp_path: Path,
) -> None:
    service, execution_service, _factory = _service(tmp_path)
    callback_base = f"{_APPROVAL_ID}:aaaaaaaa"

    detail = await service.handle_callback(
        callback_data=f"pb1:V:{callback_base}",
        callback_query_id="view-1",
        chat_id="-100123",
        user_id="88",
    )
    approved = await service.handle_callback(
        callback_data=f"pb1:A:{callback_base}",
        callback_query_id="approve-1",
        chat_id="-100123",
        user_id="88",
    )

    assert detail.detail_text is not None
    assert "Plan digest" in detail.detail_text
    assert execution_service.calls and len(execution_service.calls) == 1
    call = execution_service.calls[0]
    assert call["definition_id"] == "definition-1"
    assert call["execution_id"] == "execution-1"
    assert call["item_id"] == "item-1"
    assert call["execution_plan_id"] == "plan-1"
    assert call["approve"] is True
    assert call["decision_source"] is TaskApprovalDecisionSource.TELEGRAM
    assert call["actor"].kind == "TELEGRAM"
    assert call["actor"].subject_id == "88"
    assert call["idempotency_key"] == "telegram-approval:approve-1"
    assert approved.remove_keyboard is True


@pytest.mark.asyncio
async def test_stale_callback_expires_pending_approval(tmp_path: Path) -> None:
    service, execution_service, factory = _service(tmp_path, stale=True)

    result = await service.handle_callback(
        callback_data=f"pb1:R:{_APPROVAL_ID}:aaaaaaaa",
        callback_query_id="reject-stale",
        chat_id="-100123",
        user_id="88",
    )

    assert len(execution_service.calls) == 1
    assert result.remove_keyboard is True
    assert "过期" in result.answer_text
    with factory() as session:
        approval = session.get(TaskApprovalRecord, _APPROVAL_ID)
        assert approval is not None
        assert approval.state == TaskApprovalState.EXPIRED.value
        assert approval.actor_id == "telegram-stale-check"


@pytest.mark.asyncio
async def test_terminal_callback_does_not_reenter_decision_service(tmp_path: Path) -> None:
    service, execution_service, factory = _service(tmp_path)
    with factory() as session:
        approval = session.get(TaskApprovalRecord, _APPROVAL_ID)
        assert approval is not None
        approval.state = TaskApprovalState.APPROVED.value
        approval.decision_source = TaskApprovalDecisionSource.TELEGRAM.value
        approval.decided_at = datetime.now(UTC)
        session.commit()

    result = await service.handle_callback(
        callback_data=f"pb1:A:{_APPROVAL_ID}:aaaaaaaa",
        callback_query_id="replay-1",
        chat_id="-100123",
        user_id="88",
    )

    assert execution_service.calls == []
    assert result.answer_text == "该计划已经批准"
    assert result.remove_keyboard is True
