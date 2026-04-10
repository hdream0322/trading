from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from trading_bot.bot.context import BotContext
from trading_bot.bot.poller import TelegramPoller
from trading_bot.config import Settings, load_settings
from trading_bot.kis.client import KisClient
from trading_bot.logging_setup import setup_logging
from trading_bot.notify import telegram
from trading_bot.risk import kill_switch
from trading_bot.risk.manager import RiskManager
from trading_bot.signals.cycle import run_cycle
from trading_bot.signals.llm import ClaudeSignalClient
from trading_bot.store.db import init_db
from trading_bot.utils.calendar_kr import is_market_open_now

log = logging.getLogger("main")


def build_llm(settings: Settings) -> ClaudeSignalClient | None:
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY 비어있음 — LLM 비활성 상태로 실행")
        return None
    llm_cfg = settings.llm
    return ClaudeSignalClient(
        api_key=settings.anthropic_api_key,
        model=str(llm_cfg.get("model", "claude-haiku-4-5-20251001")),
        input_price_per_mtok=float(llm_cfg.get("input_price_per_mtok", 1.0)),
        output_price_per_mtok=float(llm_cfg.get("output_price_per_mtok", 5.0)),
        temperature=float(llm_cfg.get("temperature", 0.0)),
    )


def cycle_job(ctx: BotContext) -> None:
    if not is_market_open_now(ctx.settings.market_open, ctx.settings.market_close):
        log.info("장외 시간 — 사이클 스킵")
        return
    # 자동 사이클과 수동 /sell 경합 방지
    with ctx.trading_lock:
        try:
            run_cycle(ctx.settings, ctx.kis, ctx.llm, ctx.risk)
        except Exception:
            log.exception("사이클 실행 중 예외")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="사이클 한 번만 실행 후 종료")
    parser.add_argument("--force", action="store_true", help="장외 시간에도 강제 실행")
    args = parser.parse_args()

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    setup_logging(level="INFO", log_dir=log_dir)
    log.info("==== Trading Bot 시작 ====")

    settings = load_settings()
    log.info("KIS 거래 모드: %s / 시세 서버: %s", settings.kis.mode, settings.kis_quote.mode)
    log.info("유니버스: %d 종목, 사이클 주기: %d분", len(settings.universe), settings.cycle_minutes)

    init_db()
    llm = build_llm(settings)
    kis = KisClient(settings.kis, settings.kis_quote)
    risk = RiskManager(settings)
    ctx = BotContext(settings=settings, kis=kis, risk=risk, llm=llm)

    if kill_switch.is_active():
        log.warning("⚠️  KILL SWITCH 활성 상태로 기동 — 신규 매수 전체 차단")

    if args.once:
        try:
            if not args.force and not is_market_open_now(settings.market_open, settings.market_close):
                log.warning("장외 시간입니다. --force 로 강제 실행 가능합니다.")
                return 0
            with ctx.trading_lock:
                run_cycle(settings, kis, llm, risk)
        finally:
            kis.close()
        return 0

    # 백그라운드 Telegram 커맨드 수신기
    poller = TelegramPoller(ctx)
    poller.start()

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        cycle_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute=f"*/{settings.cycle_minutes}",
        ),
        args=[ctx],
        id="cycle",
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(*_: object) -> None:
        log.info("종료 신호 수신, scheduler 중단")
        try:
            poller.stop()
        except Exception:
            pass
        try:
            scheduler.shutdown(wait=False)
        finally:
            kis.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    badge = "🔴 실전" if settings.kis.mode == "live" else "🟡 모의"
    kill_note = " · ⚠️ 긴급 정지 켜짐" if kill_switch.is_active() else ""
    telegram.send(
        settings.telegram,
        (
            f"*봇 기동* {badge} — 점검 {settings.cycle_minutes}분 주기 (평일 09–15시){kill_note}\n"
            f"텔레그램 명령어 활성. `/help` 로 사용법 확인."
        ),
    )
    log.info("Scheduler 시작 (매 %d분)", settings.cycle_minutes)
    scheduler.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
