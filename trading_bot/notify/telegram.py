from __future__ import annotations

import json
import logging
import time
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
        if resp.status_code == 429:
            # Too Many Requests — retry_after 만큼 대기. 즉시 재시도하면 폭주.
            retry_after = 5
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                pass
            log.warning("Telegram getUpdates rate limit (429), %ds 대기", retry_after)
            time.sleep(retry_after)
            return []
        if resp.status_code >= 500:
            # 5xx 서버 오류 — 즉시 재시도하면 수십 번 폭주하므로 일정 시간 대기.
            log.warning("Telegram getUpdates 서버 오류 [%s], 5s 대기", resp.status_code)
            time.sleep(5)
            return []
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


def set_message_reaction(
    cfg: TelegramConfig,
    message_id: int,
    emoji: str | None = None,
    is_big: bool = False,
) -> bool:
    """메시지에 이모지 반응 추가/교체. emoji=None 이면 반응 제거.

    Bot API 7.0+ 의 setMessageReaction. 일반 봇은 무료 이모지 한 종류만 가능
    (Premium 봇은 여러 개). 허용 이모지: 👍 👎 ❤ 🔥 🎉 👀 👌 💔 등.
    실패해도 사용자 흐름에 지장 없으니 조용히 False 만 반환 (warning 안 띄움).
    """
    reaction: list[dict[str, Any]] = []
    if emoji:
        reaction = [{"type": "emoji", "emoji": emoji}]
    payload: dict[str, Any] = {
        "chat_id": cfg.chat_id,
        "message_id": message_id,
        "reaction": reaction,
        "is_big": is_big,
    }
    try:
        resp = _client.post(_api_url(cfg, "setMessageReaction"), json=payload)
        if resp.status_code != 200:
            log.debug(
                "Telegram setMessageReaction 실패 [%s]: %s",
                resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as exc:
        log.debug("Telegram setMessageReaction 예외: %s", exc)
        return False


def send_long(
    cfg: TelegramConfig,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    """4096자 초과 시 줄바꿈 경계 기준으로 분할해 여러 메시지로 전송.

    reply_markup 은 마지막 메시지에만 붙인다.
    분할 없이 한 메시지로 끝나면 send() 와 동일.
    """
    LIMIT = 4096
    if len(text) <= LIMIT:
        return send(cfg, text, parse_mode=parse_mode, reply_markup=reply_markup)

    # 줄바꿈 경계 기준 분할
    chunks: list[str] = []
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0

    for line in lines:
        # +1 은 줄바꿈 문자
        add_len = len(line) + 1
        if current and current_len + add_len > LIMIT:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len

    if current:
        chunks.append("\n".join(current))

    ok = True
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        ok &= send(
            cfg,
            chunk,
            parse_mode=parse_mode,
            reply_markup=reply_markup if is_last else None,
        )
    return ok


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
