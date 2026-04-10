"""Stage 4 command handlers verification.

Exercises all text commands synchronously without involving the Telegram poller.
Prints what would be sent back to the user.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from trading_bot.bot.commands import handle_callback, handle_command
from trading_bot.bot.context import BotContext
from trading_bot.config import load_settings
from trading_bot.kis.client import KisClient
from trading_bot.logging_setup import setup_logging
from trading_bot.risk import kill_switch
from trading_bot.risk.manager import RiskManager
from trading_bot.signals.llm import ClaudeSignalClient
from trading_bot.store.db import init_db


def _print(title: str, reply: dict | None) -> None:
    print(f"\n━━━ {title} ━━━")
    if reply is None:
        print("(응답 없음)")
        return
    print(reply.get("text", ""))
    rm = reply.get("reply_markup")
    if rm:
        print("[inline keyboard]")
        for row in rm.get("inline_keyboard", []):
            print("  ", " | ".join(f"{b['text']} → {b['callback_data']}" for b in row))


def main() -> int:
    setup_logging(level="WARNING", log_dir=Path("logs"))
    s = load_settings()
    init_db()

    llm = None  # 유닛 테스트에선 비활성
    if s.anthropic_api_key:
        llm = ClaudeSignalClient(
            api_key=s.anthropic_api_key,
            model=s.llm["model"],
            input_price_per_mtok=s.llm["input_price_per_mtok"],
            output_price_per_mtok=s.llm["output_price_per_mtok"],
        )

    kis = KisClient(s.kis, s.kis_quote)
    risk = RiskManager(s)
    ctx = BotContext(settings=s, kis=kis, risk=risk, llm=llm)

    try:
        # 기본 정보 계열
        _print("/help", handle_command(ctx, "/help", []))
        _print("/mode", handle_command(ctx, "/mode", []))
        _print("/universe", handle_command(ctx, "/universe", []))
        _print("/cost", handle_command(ctx, "/cost", []))

        # KIS 호출 계열
        _print("/status", handle_command(ctx, "/status", []))
        _print("/positions", handle_command(ctx, "/positions", []))
        _print("/signals", handle_command(ctx, "/signals", []))

        # 긴급 정지 토글
        print("\n━━━ 긴급 정지 상태 확인 ━━━")
        print(f"초기: {'켜짐' if kill_switch.is_active() else '꺼짐'}")

        _print("/stop", handle_command(ctx, "/stop", []))
        print(f"/stop 후: {'켜짐' if kill_switch.is_active() else '꺼짐'}")

        _print("/stop (중복)", handle_command(ctx, "/stop", []))

        _print("/resume", handle_command(ctx, "/resume", []))
        print(f"/resume 후: {'켜짐' if kill_switch.is_active() else '꺼짐'}")

        _print("/resume (중복)", handle_command(ctx, "/resume", []))

        # 알 수 없는 커맨드
        _print("/foo (unknown)", handle_command(ctx, "/foo", []))

        # /sell 인자 누락
        _print("/sell (no args)", handle_command(ctx, "/sell", []))

        # /sell 미보유 종목 (paper 계좌 비어있음)
        _print("/sell 005930 (미보유)", handle_command(ctx, "/sell", ["005930"]))

        # 콜백 핸들러
        _print("callback: cancel", handle_callback(ctx, "cancel"))
        _print("callback: positions", handle_callback(ctx, "positions"))
        _print("callback: status", handle_callback(ctx, "status"))
        _print("callback: unknown", handle_callback(ctx, "weird_data"))
    finally:
        kis.close()
        # 테스트로 건드린 킬스위치 파일이 남아있으면 정리
        if kill_switch.is_active():
            kill_switch.deactivate()

    print("\n✅ Stage 4 verification 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
