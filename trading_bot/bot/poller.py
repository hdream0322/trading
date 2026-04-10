from __future__ import annotations

import logging
import threading
import time
from typing import Any

from trading_bot.bot.commands import handle_callback, handle_command
from trading_bot.bot.context import BotContext
from trading_bot.notify import telegram

log = logging.getLogger(__name__)


class TelegramPoller:
    """Telegram Bot API long polling 기반 커맨드 수신기.

    - 백그라운드 데몬 스레드에서 동작
    - chat_id 화이트리스트 적용 (본인 채팅 외 무시)
    - 기동 시 backlog(미처리 메시지) 스킵
    - stop_event로 graceful 종료
    """

    def __init__(self, ctx: BotContext):
        self.ctx = ctx
        self.allowed_chat_id = str(ctx.settings.telegram.chat_id)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._loop, name="telegram-poller", daemon=True
        )
        self.thread.start()
        log.info("Telegram poller 스레드 시작 (chat_id allowlist=%s)", self.allowed_chat_id)

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        offset = self._get_initial_offset()
        log.info("Telegram poller initial offset=%d (backlog 스킵)", offset)
        while not self.stop_event.is_set():
            try:
                updates = telegram.get_updates(
                    self.ctx.settings.telegram, offset=offset, timeout=30
                )
                for update in updates:
                    offset = int(update.get("update_id", offset)) + 1
                    try:
                        self._handle_update(update)
                    except Exception:
                        log.exception("update 처리 중 예외: %s", update.get("update_id"))
            except Exception:
                log.exception("polling 루프 예외, 5초 후 재시도")
                if self.stop_event.wait(5):
                    break

    def _get_initial_offset(self) -> int:
        """기동 시 쌓여있는 backlog를 스킵해서 오래된 커맨드가 실행되지 않도록 한다."""
        try:
            latest = telegram.get_updates(
                self.ctx.settings.telegram, offset=-1, timeout=0
            )
            if latest:
                return int(latest[-1]["update_id"]) + 1
        except Exception:
            log.warning("initial offset 조회 실패, 0부터 시작")
        return 0

    def _is_allowed(self, from_id: Any) -> bool:
        return str(from_id) == self.allowed_chat_id

    def _handle_update(self, update: dict[str, Any]) -> None:
        # Text message (커맨드)
        if "message" in update:
            msg = update["message"]
            from_id = msg.get("from", {}).get("id", "")
            if not self._is_allowed(from_id):
                log.warning("미인가 chat_id=%s 메시지 무시", from_id)
                return
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                return
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()  # /help@bot_name → /help
            args = parts[1:]
            log.info("커맨드 수신: %s %s", cmd, args)
            reply = handle_command(self.ctx, cmd, args)
            if reply:
                telegram.send(
                    self.ctx.settings.telegram,
                    reply.get("text", ""),
                    reply_markup=reply.get("reply_markup"),
                )
            return

        # Callback query (inline 버튼 탭)
        if "callback_query" in update:
            cq = update["callback_query"]
            from_id = cq.get("from", {}).get("id", "")
            cq_id = cq.get("id", "")
            if not self._is_allowed(from_id):
                log.warning("미인가 callback chat_id=%s 무시", from_id)
                telegram.answer_callback(
                    self.ctx.settings.telegram, cq_id, "권한 없음", show_alert=True
                )
                return
            data = cq.get("data", "")
            log.info("콜백 수신: %s", data)
            # 먼저 answerCallbackQuery로 버튼 로딩 스피너 해제
            telegram.answer_callback(self.ctx.settings.telegram, cq_id, "")
            reply = handle_callback(self.ctx, data)
            if reply:
                telegram.send(
                    self.ctx.settings.telegram,
                    reply.get("text", ""),
                    reply_markup=reply.get("reply_markup"),
                )
            return
