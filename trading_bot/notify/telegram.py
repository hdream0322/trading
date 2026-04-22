from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from trading_bot.config import TelegramConfig

log = logging.getLogger(__name__)


# 모듈 전역 httpx.Client — 연결/TLS 핸드셰이크 재사용.
# 매 호출마다 새 TCP+TLS 를 세우면 KIS 버스트 직후 sendMessage 가 10초 안에
# 못 붙어 timed out 으로 떨어지는 문제가 발생함. Client 재사용으로 handshake
# 비용을 1회로 고정.
# connect=5s 는 DNS+TCP+TLS 여유, read=20s 는 Telegram API 혼잡 구간 흡수.
_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_client = httpx.Client(
    timeout=_DEFAULT_TIMEOUT,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    http2=False,
)


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
        resp = _client.post(_api_url(cfg, "sendMessage"), json=payload)
        if resp.status_code != 200:
            log.warning("Telegram sendMessage 실패 [%s]: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        log.warning("Telegram sendMessage 예외: %s", exc)
        return False


def send_document(
    cfg: TelegramConfig,
    filename: str,
    content: bytes,
    caption: str | None = None,
) -> bool:
    """파일 첨부 전송 (sendDocument). Telegram 업로드 한도 50MB."""
    data: dict[str, Any] = {"chat_id": cfg.chat_id}
    if caption:
        # caption 은 최대 1024자
        data["caption"] = caption[:1024]
        data["parse_mode"] = "Markdown"
    files = {"document": (filename, content, "application/octet-stream")}
    try:
        # 파일 업로드는 큰 페이로드라 read timeout 60s 로 개별 상향.
        resp = _client.post(
            _api_url(cfg, "sendDocument"),
            data=data,
            files=files,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
        if resp.status_code != 200:
            log.warning("Telegram sendDocument 실패 [%s]: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        log.warning("Telegram sendDocument 예외: %s", exc)
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
        # long polling 은 서버가 timeout 초만큼 잡고 있을 수 있으므로 read 를 그에 +10.
        resp = _client.get(
            _api_url(cfg, "getUpdates"),
            params=params,
            timeout=httpx.Timeout(float(timeout + 10), connect=5.0),
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


def set_commands(cfg: TelegramConfig, commands: list[tuple[str, str]]) -> bool:
    """Telegram 사용자가 `/` 입력 시 표시되는 자동완성 메뉴를 등록.

    commands: [(command_name_without_slash, description), ...]
    description 은 1~256자, command 는 소문자 영문+숫자+언더스코어만 허용.
    이 호출은 idempotent — 봇 기동 시마다 호출해도 무방.
    """
    payload = {
        "commands": [
            {"command": cmd, "description": desc}
            for cmd, desc in commands
        ]
    }
    try:
        resp = _client.post(_api_url(cfg, "setMyCommands"), json=payload)
        if resp.status_code != 200:
            log.warning("Telegram setMyCommands 실패 [%s]: %s", resp.status_code, resp.text)
            return False
        log.info("Telegram 커맨드 메뉴 등록 완료 (%d개)", len(commands))
        return True
    except Exception as exc:
        log.warning("Telegram setMyCommands 예외: %s", exc)
        return False


def delete_message(cfg: TelegramConfig, message_id: int) -> bool:
    """특정 메시지 삭제. 주로 시크릿을 포함한 사용자 메시지 (/setcreds) 처리용.

    Bot API 제약: private 채팅에서 봇은 자신의 메시지는 언제든, 사용자 메시지는
    48시간 이내에만 삭제 가능. /setcreds 는 즉시 삭제하므로 문제 없음.
    """
    payload = {
        "chat_id": cfg.chat_id,
        "message_id": message_id,
    }
    try:
        resp = _client.post(_api_url(cfg, "deleteMessage"), json=payload)
        if resp.status_code != 200:
            log.warning(
                "Telegram deleteMessage 실패 [%s]: %s",
                resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as exc:
        log.warning("Telegram deleteMessage 예외: %s", exc)
        return False


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
        resp = _client.post(_api_url(cfg, "answerCallbackQuery"), json=payload)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("Telegram answerCallbackQuery 예외: %s", exc)
        return False
