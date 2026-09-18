from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.task_definition_executions import TaskDefinitionExecutionService
from backend.app.domain.task_approval import (
    TaskApprovalDecisionSource,
    TaskApprovalState,
)
from backend.app.infrastructure.persistence.models import (
    Site,
    TaskApprovalRecord,
    TaskDefinition,
    TaskExecution,
    TaskExecutionItem,
    TaskRiskSummaryRecord,
    utc_now,
)

_CALLBACK_RE = re.compile(
    r"^pb1:(?P<action>[ARV]):(?P<approval>[0-9a-fA-F-]{36}):(?P<digest>[0-9a-fA-F]{8})$"
)


@dataclass(frozen=True, slots=True)
class TelegramApprovalRequest:
    approval_id: str
    execution_id: str
    item_id: str
    task_name: str
    site_name: str | None
    execution_plan_id: str
    plan_digest: str
    risk_level: str
    reason_codes: tuple[str, ...]
    action_kinds: tuple[str, ...]
    hardlink_count: int
    client_fetch_count: int
    create_directory_count: int
    estimated_download_bytes_upper_bound: int
    message_text: str
    reply_markup: dict[str, object]


@dataclass(frozen=True, slots=True)
class TelegramApprovalCallbackResult:
    answer_text: str
    detail_text: str | None = None
    remove_keyboard: bool = False


@dataclass(frozen=True, slots=True)
class _ApprovalContext:
    approval: TaskApprovalRecord
    risk: TaskRiskSummaryRecord
    execution: TaskExecution
    item: TaskExecutionItem
    definition: TaskDefinition
    site: Site | None


class TaskTelegramApprovalService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        execution_service: TaskDefinitionExecutionService,
    ) -> None:
        self._session_factory = session_factory
        self._execution_service = execution_service

    def list_pending_requests(self, *, limit: int = 20) -> tuple[TelegramApprovalRequest, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Telegram approval limit 必须位于 1..100")
        with self._session_factory() as session:
            approvals = tuple(
                session.scalars(
                    select(TaskApprovalRecord)
                    .where(
                        TaskApprovalRecord.state == TaskApprovalState.PENDING.value,
                        TaskApprovalRecord.telegram_notified_at.is_(None),
                    )
                    .order_by(TaskApprovalRecord.created_at, TaskApprovalRecord.id)
                    .limit(limit)
                )
            )
            requests: list[TelegramApprovalRequest] = []
            for approval in approvals:
                context = self._context(session, approval)
                if context is None:
                    continue
                requests.append(self._request_view(context))
            return tuple(requests)

    def mark_notified(
        self,
        approval_id: str,
        *,
        chat_id: str,
        message_id: str | None,
    ) -> bool:
        with self._session_factory() as session:
            approval = session.get(TaskApprovalRecord, approval_id)
            if (
                approval is None
                or approval.state != TaskApprovalState.PENDING.value
                or approval.telegram_notified_at is not None
            ):
                return False
            approval.telegram_notified_at = utc_now()
            approval.telegram_chat_id = chat_id
            approval.telegram_message_id = message_id
            approval.updated_at = utc_now()
            session.commit()
            return True

    async def handle_callback(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: str | None,
        user_id: str | None,
    ) -> TelegramApprovalCallbackResult:
        parsed = _CALLBACK_RE.fullmatch(callback_data.strip())
        if parsed is None:
            return TelegramApprovalCallbackResult(
                "审批按钮无效或版本不受支持",
                remove_keyboard=False,
            )
        approval_id = parsed.group("approval").lower()
        action = parsed.group("action")
        digest_prefix = parsed.group("digest").lower()
        with self._session_factory() as session:
            approval = session.get(TaskApprovalRecord, approval_id)
            if approval is None:
                return TelegramApprovalCallbackResult(
                    "审批记录已不存在",
                    remove_keyboard=True,
                )
            context = self._context(session, approval)
            if context is None:
                return TelegramApprovalCallbackResult(
                    "审批上下文已不存在",
                    remove_keyboard=True,
                )
            if approval.plan_digest[:8].lower() != digest_prefix:
                return TelegramApprovalCallbackResult(
                    "审批计划校验失败",
                    remove_keyboard=True,
                )
            terminal = self._terminal_result(context, action=action)
            if terminal is not None:
                return terminal
            if action == "V":
                return TelegramApprovalCallbackResult(
                    "已发送审批详情",
                    detail_text=self._detail_text(context),
                )
            definition_id = context.definition.id
            execution_id = context.execution.id
            item_id = context.item.id
            execution_plan_id = approval.execution_plan_id

        actor_subject = user_id or chat_id
        if actor_subject is None:
            return TelegramApprovalCallbackResult("无法确认审批身份")
        approve = action == "A"
        try:
            await self._execution_service.decide_execution_approval(
                definition_id,
                execution_id,
                item_id,
                execution_plan_id=execution_plan_id,
                approve=approve,
                note="Telegram callback approval",
                actor=TaskActionActor("TELEGRAM", actor_subject),
                idempotency_key=f"telegram-approval:{callback_query_id}",
                decision_source=TaskApprovalDecisionSource.TELEGRAM,
            )
        except ApplicationError as exc:
            if exc.code in {"TASK_APPROVAL_PLAN_STALE", "TASK_APPROVAL_NOT_READY"}:
                self._expire_pending(approval_id, reason=exc.code)
                return TelegramApprovalCallbackResult(
                    "执行计划已变化，本次审批已过期",
                    remove_keyboard=True,
                )
            if exc.code == "TASK_APPROVAL_ALREADY_DECIDED":
                return self._reload_terminal(approval_id)
            return TelegramApprovalCallbackResult(
                "审批处理失败，请在 Web 中查看任务状态",
                remove_keyboard=False,
            )
        return TelegramApprovalCallbackResult(
            "已批准本次执行" if approve else "已拒绝本次执行",
            remove_keyboard=True,
        )

    def _reload_terminal(self, approval_id: str) -> TelegramApprovalCallbackResult:
        with self._session_factory() as session:
            approval = session.get(TaskApprovalRecord, approval_id)
            if approval is None:
                return TelegramApprovalCallbackResult("审批记录已不存在", remove_keyboard=True)
            context = self._context(session, approval)
            if context is None:
                return TelegramApprovalCallbackResult("审批上下文已不存在", remove_keyboard=True)
            return self._terminal_result(context, action="V") or TelegramApprovalCallbackResult(
                "审批状态已变化，请重试",
                remove_keyboard=False,
            )

    def _expire_pending(self, approval_id: str, *, reason: str) -> None:
        with self._session_factory() as session:
            approval = session.get(TaskApprovalRecord, approval_id)
            if approval is None or approval.state != TaskApprovalState.PENDING.value:
                return
            now = utc_now()
            approval.state = TaskApprovalState.EXPIRED.value
            approval.actor_kind = "SYSTEM"
            approval.actor_id = "telegram-stale-check"
            approval.decision_note = reason
            approval.decided_at = now
            approval.updated_at = now
            session.commit()

    @staticmethod
    def _terminal_result(
        context: _ApprovalContext,
        *,
        action: str,
    ) -> TelegramApprovalCallbackResult | None:
        state = context.approval.state
        if state == TaskApprovalState.PENDING.value:
            return None
        labels = {
            TaskApprovalState.APPROVED.value: "该计划已经批准",
            TaskApprovalState.REJECTED.value: "该计划已经拒绝",
            TaskApprovalState.EXPIRED.value: "该计划已经过期",
        }
        return TelegramApprovalCallbackResult(
            labels.get(state, "审批已经结束"),
            detail_text=(
                TaskTelegramApprovalService._detail_text(context) if action == "V" else None
            ),
            remove_keyboard=True,
        )

    @staticmethod
    def _context(session: Session, approval: TaskApprovalRecord) -> _ApprovalContext | None:
        risk = session.get(TaskRiskSummaryRecord, approval.risk_summary_id)
        if risk is None:
            return None
        row = session.execute(
            select(TaskExecutionItem, TaskExecution)
            .join(TaskExecution, TaskExecution.id == TaskExecutionItem.execution_id)
            .where(TaskExecutionItem.unpack_task_id == approval.task_id)
            .order_by(TaskExecution.created_at.desc(), TaskExecutionItem.created_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        item, execution = row
        if execution.task_definition_id is None:
            return None
        definition = session.get(TaskDefinition, execution.task_definition_id)
        if definition is None:
            return None
        site = session.get(Site, definition.site_id) if definition.site_id is not None else None
        return _ApprovalContext(approval, risk, execution, item, definition, site)

    @staticmethod
    def _request_view(context: _ApprovalContext) -> TelegramApprovalRequest:
        approval = context.approval
        digest = approval.plan_digest[:8].lower()
        callback_base = f"{approval.id}:{digest}"
        reply_markup: dict[str, object] = {
            "inline_keyboard": [
                [
                    {"text": "✅ 批准本次", "callback_data": f"pb1:A:{callback_base}"},
                    {"text": "❌ 拒绝执行", "callback_data": f"pb1:R:{callback_base}"},
                ],
                [{"text": "🔍 查看详情", "callback_data": f"pb1:V:{callback_base}"}],
            ]
        }
        return TelegramApprovalRequest(
            approval_id=approval.id,
            execution_id=context.execution.id,
            item_id=context.item.id,
            task_name=context.execution.task_name,
            site_name=context.site.name if context.site is not None else None,
            execution_plan_id=approval.execution_plan_id,
            plan_digest=approval.plan_digest,
            risk_level=context.risk.risk_level,
            reason_codes=tuple(context.risk.reason_codes),
            action_kinds=tuple(context.risk.action_kinds),
            hardlink_count=context.risk.hardlink_count,
            client_fetch_count=context.risk.client_fetch_count,
            create_directory_count=context.risk.create_directory_count,
            estimated_download_bytes_upper_bound=(
                context.risk.estimated_download_bytes_upper_bound
            ),
            message_text=TaskTelegramApprovalService._message_text(context),
            reply_markup=reply_markup,
        )

    @staticmethod
    def _message_text(context: _ApprovalContext) -> str:
        site = context.site.name if context.site is not None else "—"
        actions = ", ".join(context.risk.action_kinds) or "—"
        reasons = ", ".join(context.risk.reason_codes) or "—"
        estimated = TaskTelegramApprovalService._format_bytes(
            context.risk.estimated_download_bytes_upper_bound
        )
        return (
            "⚠️ PackBreaker 高风险操作待确认\n\n"
            f"任务：{context.execution.task_name}\n"
            f"扫描站点：{site}\n"
            f"风险等级：{context.risk.risk_level}\n"
            f"Action：{actions}\n"
            f"风险原因：{reasons}\n"
            f"硬链接：{context.risk.hardlink_count}\n"
            f"客户端下载：{context.risk.client_fetch_count}\n"
            f"创建目录：{context.risk.create_directory_count}\n"
            f"预计最多下载：{estimated}\n\n"
            "当前未执行任何真实数据修改。"
        )

    @staticmethod
    def _detail_text(context: _ApprovalContext) -> str:
        return (
            TaskTelegramApprovalService._message_text(context)
            + "\n\n"
            + f"Execution Plan：{context.approval.execution_plan_id}\n"
            + f"Plan digest：{context.approval.plan_digest}"
        )

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = max(0, value)
        units = ("B", "KB", "MB", "GB", "TB")
        amount = float(size)
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.2f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{size} B"
