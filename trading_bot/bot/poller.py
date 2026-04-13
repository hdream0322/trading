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

            # 한 말풍선에 여러 줄 커맨드가 올 수 있음 — 줄 단위로 쪼개 순차 실행.
            # 예) "/status\n/positions\n/cost" → 세 개 각각 별도 커맨드로 처리.
            # 한 줄에서 예외가 터져도 나머지 줄은 계속 실행 (격리).
            # `/` 로 시작하지 않는 줄은 스킵.
            command_lines = [
                line.strip() for line in text.splitlines() if line.strip().startswith("/")
            ]
            total = len(command_lines)
            is_multi = total > 1
            success_count = 0
            fail_count = 0
            should_delete_original = False

            for idx, line in enumerate(command_lines, start=1):
                parts = line.split()
                cmd = parts[0].split("@")[0].lower()  # /help@bot_name → /help
                args = parts[1:]
                # /setcreds 는 로그에 args 남기지 말 것 (시크릿 포함)
                log_args = "[REDACTED]" if cmd == "/setcreds" and args else args
                log.info("커맨드 수신 (%d/%d): %s %s", idx, total, cmd, log_args)

                # 1. 핸들러 실행 — handle_command 는 자체 try/except 가 있어
                #    거의 항상 reply dict 를 돌려주지만, 만일을 대비해 한 번 더 감쌈.
                try:
                    reply = handle_command(self.ctx, cmd, args)
                except Exception as exc:
                    log.exception("커맨드 처리 중 예외: %s", cmd)
                    reply = {
                        "text": f"❌ `{cmd}` 처리 중 오류\n`{type(exc).__name__}: {exc}`",
                    }

                if not reply:
                    # 핸들러가 None 돌려주는 케이스 (이례적) — 성공 집계
                    success_count += 1
                    continue

                reply_text = reply.get("text", "") or ""
                is_error_reply = reply_text.startswith("❌") or reply_text.startswith(
                    "모르는 명령어"
                )
                if is_error_reply:
                    fail_count += 1
                else:
                    success_count += 1

                # 2. 멀티커맨드일 때만 번호표 접두사 붙임 (단일 커맨드는 기존 동작 유지)
                out_text = reply_text
                if is_multi:
                    marker = "⛔" if is_error_reply else "🔹"
                    out_text = f"{marker} *({idx}/{total})* `{cmd}`\n{reply_text}"

                # 3. 응답 전송 — document 첨부 응답이면 sendDocument, 아니면 sendMessage
                try:
                    doc = reply.get("document")
                    if doc:
                        filename, content = doc
                        telegram.send_document(
                            self.ctx.settings.telegram,
                            filename,
                            content,
                            caption=out_text or None,
                        )
                    else:
                        telegram.send(
                            self.ctx.settings.telegram,
                            out_text,
                            reply_markup=reply.get("reply_markup"),
                        )
                except Exception:
                    log.exception("응답 전송 실패: %s", cmd)

                # 4. 시크릿 포함 원본 메시지 삭제 플래그
                #    한 줄이라도 요구하면 원본 전체 삭제 (시크릿이 말풍선에 남는 걸 막는 게 목적).
                if reply.get("delete_original"):
                    should_delete_original = True

            # 5. 멀티커맨드일 땐 마지막에 요약 한 줄
            if is_multi:
                if fail_count == 0:
                    summary = (
                        f"━━━━━━━━━━━\n"
                        f"✅ *{total}개 커맨드 모두 성공*"
                    )
                else:
                    summary = (
                        f"━━━━━━━━━━━\n"
                        f"{total}개 중 *{success_count}개 성공*, "
                        f"*{fail_count}개 실패*"
                    )
                try:
                    telegram.send(self.ctx.settings.telegram, summary)
                except Exception:
                    log.exception("요약 전송 실패")

            # 6. 시크릿 원본 메시지 삭제
            if should_delete_original:
                msg_id = msg.get("message_id")
                if msg_id:
                    try:
                        telegram.delete_message(
                            self.ctx.settings.telegram, int(msg_id)
                        )
                    except Exception:
                        log.exception("원본 메시지 삭제 실패")
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
                doc = reply.get("document")
                if doc:
                    filename, content = doc
                    telegram.send_document(
                        self.ctx.settings.telegram,
                        filename,
                        content,
                        caption=reply.get("text") or None,
                    )
                else:
                    telegram.send(
                        self.ctx.settings.telegram,
                        reply.get("text", ""),
                        reply_markup=reply.get("reply_markup"),
                    )
            return
