from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from backend.app.application.ai_telegram import AITelegramBindingView, AITelegramService
from backend.app.application.notifications import NotificationService
from backend.app.application.task_telegram_approvals import TaskTelegramApprovalService
from backend.app.infrastructure.adapters.telegram_ai import (
    TelegramAIClient,
    TelegramAIClientFactory,
    TelegramAIError,
    TelegramInboundUpdate,
)

_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX_MESSAGES = 10


class TelegramAIClientFactoryPort(Protocol):
    def create(self, *, bot_token: str, proxy_url: str | None) -> TelegramAIClient: ...


@dataclass(frozen=True, slots=True)
class AITelegramDriverReport:
    scanned_count: int
    authorized_count: int
    replied_count: int
    failed_count: int
    rate_limited_count: int
    last_update_id: int
    approval_requested_count: int = 0
    approval_processed_count: int = 0


@dataclass(frozen=True, slots=True)
class AITelegramDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    consecutive_errors: int
    last_error_code: str | None
    last_report: AITelegramDriverReport | None


class AITelegramDriver:
    """独立 Telegram Long Polling Driver；失败不影响拆包/通知 Driver。"""

    def __init__(
        self,
        service: AITelegramService,
        notification_service: NotificationService,
        *,
        interval_seconds: float = 1.0,
        poll_timeout_seconds: int = 20,
        poll_limit: int = 20,
        client_factory: TelegramAIClientFactoryPort | None = None,
        approval_service: TaskTelegramApprovalService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("AI Telegram driver interval 必须大于 0")
        if not 5 <= poll_timeout_seconds <= 50:
            raise ValueError("AI Telegram poll timeout 必须在 5～50 秒之间")
        if not 1 <= poll_limit <= 100:
            raise ValueError("AI Telegram poll limit 必须在 1～100 之间")
        self._service = service
        self._notification_service = notification_service
        self._interval_seconds = interval_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_limit = poll_limit
        self._client_factory = client_factory or TelegramAIClientFactory()
        self._approval_service = approval_service
        self._logger = logger or logging.getLogger("packbreaker.ai.telegram")
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._ticks_started = 0
        self._ticks_completed = 0
        self._consecutive_errors = 0
        self._last_error_code: str | None = None
        self._last_report: AITelegramDriverReport | None = None
        self._rate_windows: dict[str, deque[float]] = defaultdict(deque)

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> AITelegramDriverState:
        return AITelegramDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            consecutive_errors=self._consecutive_errors,
            last_error_code=self._last_error_code,
            last_report=self._last_report,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._run_loop(), name="packbreaker-ai-telegram-driver")

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._stop_event.set()
        if runner is None:
            return
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner

    async def run_once(self) -> AITelegramDriverReport | None:
        if self._tick_lock.locked():
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        try:
            report = await self._poll_once()
        except asyncio.CancelledError:
            raise
        except TelegramAIError as exc:
            self._consecutive_errors += 1
            self._last_error_code = exc.code
            raise
        except Exception:
            self._consecutive_errors += 1
            self._last_error_code = "AI_TELEGRAM_DRIVER_ERROR"
            raise
        else:
            self._ticks_completed += 1
            self._consecutive_errors = 0
            self._last_error_code = None
            self._last_report = report
            return report
        finally:
            self._tick_lock.release()

    async def _poll_once(self) -> AITelegramDriverReport:
        binding = self._service.get()
        if (
            not binding.enabled
            and not binding.approval_enabled
            or binding.notification_channel_id is None
        ):
            return AITelegramDriverReport(0, 0, 0, 0, 0, binding.last_update_id)
        runtime = self._notification_service.telegram_ai_runtime(binding.notification_channel_id)
        client = self._client_factory.create(
            bot_token=runtime.credential.bot_token,
            proxy_url=runtime.proxy_url,
        )
        approval_requested = 0
        if binding.approval_enabled:
            if self._approval_service is None:
                raise RuntimeError("Telegram approval 已启用但审批服务未配置")
            for request in self._approval_service.list_pending_requests(limit=self._poll_limit):
                sent = await client.send_message(
                    chat_id=runtime.credential.chat_id,
                    text=request.message_text,
                    reply_markup=request.reply_markup,
                )
                if self._approval_service.mark_notified(
                    request.approval_id,
                    chat_id=sent.chat_id,
                    message_id=sent.message_id,
                ):
                    approval_requested += 1
        updates = await client.poll(
            offset=binding.last_update_id + 1,
            timeout_seconds=self._poll_timeout_seconds,
            limit=self._poll_limit,
        )
        scanned = 0
        authorized = 0
        replied = 0
        failed = 0
        rate_limited = 0
        approval_processed = 0
        last_update_id = binding.last_update_id
        for update in updates:
            scanned += 1
            try:
                if update.callback_query_id is not None or update.callback_data is not None:
                    if update.callback_query_id is None:
                        failed += 1
                        continue
                    if not binding.approval_enabled or self._approval_service is None:
                        await client.answer_callback_query(
                            callback_query_id=update.callback_query_id,
                            text="Telegram 审批未启用",
                            show_alert=True,
                        )
                        continue
                    if not self._service.is_authorized(binding, update):
                        await client.answer_callback_query(
                            callback_query_id=update.callback_query_id,
                            text="当前 Telegram 身份没有审批权限",
                            show_alert=True,
                        )
                        continue
                    authorized += 1
                    if update.callback_data is None:
                        await client.answer_callback_query(
                            callback_query_id=update.callback_query_id,
                            text="审批按钮数据无效",
                            show_alert=True,
                        )
                        failed += 1
                        continue
                    callback = await self._approval_service.handle_callback(
                        callback_data=update.callback_data,
                        callback_query_id=update.callback_query_id,
                        chat_id=update.chat_id,
                        user_id=update.user_id,
                    )
                    approval_processed += 1
                    await client.answer_callback_query(
                        callback_query_id=update.callback_query_id,
                        text=callback.answer_text,
                        show_alert=False,
                    )
                    if callback.detail_text is not None and update.chat_id is not None:
                        await client.send_message(
                            chat_id=update.chat_id,
                            text=callback.detail_text,
                        )
                        replied += 1
                    if (
                        callback.remove_keyboard
                        and update.chat_id is not None
                        and update.message_id is not None
                    ):
                        await client.clear_inline_keyboard(
                            chat_id=update.chat_id,
                            message_id=update.message_id,
                        )
                    continue
                if not binding.enabled:
                    continue
                if self._service.is_authorized(binding, update) and update.text:
                    authorized += 1
                    if self._is_rate_limited(binding, update):
                        rate_limited += 1
                        if update.chat_id is not None:
                            await client.send_message(
                                chat_id=update.chat_id,
                                text="请求过于频繁，请稍后再试。",
                            )
                            replied += 1
                        continue
                result = await self._service.process_update(binding, update)
                if result.reply_text is not None and update.chat_id is not None:
                    await client.send_message(chat_id=update.chat_id, text=result.reply_text)
                    replied += 1
                if result.error_code is not None:
                    failed += 1
            except TelegramAIError:
                failed += 1
            except Exception:
                failed += 1
                self._logger.exception(
                    "Telegram AI 单条消息处理失败 update_id=%s",
                    update.update_id,
                )
            finally:
                self._service.advance_cursor(update.update_id)
                last_update_id = max(last_update_id, update.update_id)
        return AITelegramDriverReport(
            scanned_count=scanned,
            authorized_count=authorized,
            replied_count=replied,
            failed_count=failed,
            rate_limited_count=rate_limited,
            last_update_id=last_update_id,
            approval_requested_count=approval_requested,
            approval_processed_count=approval_processed,
        )

    def _is_rate_limited(
        self,
        binding: AITelegramBindingView,
        update: TelegramInboundUpdate,
    ) -> bool:
        key = f"{binding.id}:{update.chat_id or '-'}:{update.user_id or '-'}"
        now = time.monotonic()
        window = self._rate_windows[key]
        cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= _RATE_LIMIT_MAX_MESSAGES:
            return True
        window.append(now)
        return False

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "Telegram AI Driver 轮询失败 error_code=%s consecutive_errors=%s",
                    self._last_error_code,
                    self._consecutive_errors,
                )
            delay = min(
                60.0,
                self._interval_seconds * (2 ** min(self._consecutive_errors, 6)),
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
