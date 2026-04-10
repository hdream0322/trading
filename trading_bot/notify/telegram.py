from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from trading_bot.config import TelegramConfig

log = logging.getLogger(__name__)


def _api_url(cfg: TelegramConfig, method: str) -> str:
    return f"https://api.telegram.org/bot{cfg.bot_token}/{method}"


def send(
    cfg: TelegramConfig,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        resp = httpx.post(_api_url(cfg, "sendMessage"), json=payload, timeout=10)
        if resp.status_code != 200:
            log.warning("Telegram sendMessage 실패 [%s]: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        log.warning("Telegram sendMessage 예외: %s", exc)
        return False


def get_updates(
    cfg: TelegramConfig,
    offset: int = 0,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Long polling getUpdates. timeout 동안 새 메시지를 기다리며 block."""
    params = {
        "offset": offset,
        "timeout": timeout,
        # 콜백 쿼리도 받기 위해 allowed_updates 명시
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    try:
        resp = httpx.get(
            _api_url(cfg, "getUpdates"),
            params=params,
            timeout=timeout + 10,
        )
        if resp.status_code != 200:
            log.warning("Telegram getUpdates 실패 [%s]: %s", resp.status_code, resp.text)
            return []
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram getUpdates ok=false: %s", data)
            return []
        return list(data.get("result", []))
    except httpx.ReadTimeout:
        # long polling 타임아웃은 정상 — 그냥 빈 배열 반환
        return []
    except Exception as exc:
        log.warning("Telegram getUpdates 예외: %s", exc)
        return []


def answer_callback(
    cfg: TelegramConfig,
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> bool:
    """inline 버튼 탭 시 로딩 스피너 해제 + 토스트 메시지."""
    payload = {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert,
    }
    try:
        resp = httpx.post(
            _api_url(cfg, "answerCallbackQuery"),
            json=payload,
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as exc:
        log.warning("Telegram answerCallbackQuery 예외: %s", exc)
        return False
