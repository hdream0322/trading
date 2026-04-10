from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from trading_bot.bot import expiry, runtime_state, update_manager
from trading_bot.bot.commands import TELEGRAM_BOT_COMMANDS
from trading_bot.bot.context import BotContext
from trading_bot.bot.poller import TelegramPoller
from trading_bot.config import (
    CREDENTIALS_OVERRIDE_FILE,
    Settings,
    build_trade_cfg,
    load_credentials_override,
    load_settings,
)
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


def auto_update_job(ctx: BotContext) -> None:
    """매일 02:00 KST — 자동 업데이트 스케줄 훅.

    상태 파일(data/AUTO_UPDATE_DISABLED) 검사 후 활성 상태면 Watchtower
    HTTP API 호출. 비활성 상태면 로그만 남기고 스킵.
    """
    if not update_manager.is_auto_enabled():
        log.info("자동 업데이트 비활성 상태 — 02:00 스케줄 스킵")
        return
    try:
        update_manager.trigger_update(ctx.settings.watchtower_http_token)
        log.info("자동 업데이트 요청 전송 완료 (Watchtower 비동기 처리)")
    except Exception:
        log.exception("자동 업데이트 요청 실패")


def paper_expiry_check_job(ctx: BotContext) -> None:
    """매일 08:00 KST — KIS 모의투자 계좌 만료 임박 여부 확인.

    paper 모드일 때만 작동. 만료 7일 이내면 텔레그램 경고 전송.
    이미 만료된 상태라면 매일 한 번씩 경고를 계속 보냄 (사용자가 재신청할 때까지).
    """
    if ctx.settings.kis.mode != "paper":
        return
    days_left = expiry.days_until_expiry()
    message = expiry.build_expiry_warning(days_left, ctx.settings.kis.mode)
    if message:
        log.warning("paper 만료 경고: 남은 %s일", days_left)
        telegram.send(ctx.settings.telegram, message)


def credentials_watcher_job(ctx: BotContext) -> None:
    """5분마다 data/credentials.env 의 mtime 을 확인, 변경 감지 시 자동 재로드.

    사용자가 새 자격증명을 파일에 저장하면 /reload 커맨드 없이도 5분 내
    자동 반영된다. 3개월 갱신 시 사용자 경험 개선용.

    mtime 상태는 runtime_state 모듈에 있어서 /setcreds 가 파일을 수정한
    뒤에도 동기화할 수 있다 (중복 재로드 방지).
    """
    if not CREDENTIALS_OVERRIDE_FILE.exists():
        return
    try:
        current_mtime = CREDENTIALS_OVERRIDE_FILE.stat().st_mtime
    except OSError:
        return

    if runtime_state.credentials_last_mtime == 0.0:
        # 최초 관찰 — baseline 으로만 기록하고 리로드 안 함
        runtime_state.credentials_last_mtime = current_mtime
        return
    if current_mtime == runtime_state.credentials_last_mtime:
        return  # 변경 없음

    log.info(
        "credentials.env 변경 감지 (%s → %s) 자동 재로드 시작",
        runtime_state.credentials_last_mtime, current_mtime,
    )
    runtime_state.credentials_last_mtime = current_mtime

    try:
        load_credentials_override()
        new_cfg = build_trade_cfg(ctx.settings.kis.mode)
    except Exception as exc:
        log.exception("자동 credentials 재로드 실패")
        telegram.send(
            ctx.settings.telegram,
            f"❌ *자격증명 자동 재로드 실패*\n"
            f"credentials.env 변경을 감지했지만 로드 실패.\n"
            f"`{exc}`",
        )
        return

    # 토큰 캐시 삭제
    from pathlib import Path
    tokens_dir = Path(__file__).resolve().parent.parent / "tokens"
    deleted = 0
    try:
        for token_file in tokens_dir.glob("kis_token_*.json"):
            token_file.unlink()
            deleted += 1
    except Exception:
        pass

    # KisClient 원자적 교체
    with ctx.trading_lock:
        old_kis = ctx.kis
        new_kis = KisClient(new_cfg, ctx.settings.kis_quote)
        ctx.settings.kis = new_cfg
        ctx.kis = new_kis
        try:
            old_kis.close()
        except Exception:
            pass

    # paper 모드면 만료 카운트다운 리셋
    if new_cfg.mode == "paper":
        expiry.mark_updated()

    log.info("자동 credentials 재로드 완료: %s 계좌 %s",
             new_cfg.mode, new_cfg.account_no)
    telegram.send(
        ctx.settings.telegram,
        f"✅ *자격증명 자동 재로드*\n"
        f"`credentials.env` 변경 감지됨.\n\n"
        f"새 계좌: `{new_cfg.account_no}-{new_cfg.account_product_cd}`\n"
        f"앱키 앞 12자: `{new_cfg.app_key[:12]}...`\n"
        f"토큰 캐시 삭제: {deleted}개\n"
        f"다음 KIS 호출부터 새 키로 동작합니다.",
    )


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

    # 기동 시점의 GHCR :latest digest 를 스냅샷 → 이후 /update 의
    # "최신 여부" 비교 기준. 네트워크 실패해도 봇 기동은 계속.
    update_manager.snapshot_current_digest()

    # paper 자격증명 만료 카운트다운 초기화 (파일 없는 경우만 생성)
    expiry.ensure_issued_date()

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

    # Telegram 커맨드 자동완성 메뉴 등록 (사용자가 `/` 입력 시 목록 표시)
    telegram.set_commands(settings.telegram, TELEGRAM_BOT_COMMANDS)

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
    # 자동 업데이트 — 매일 02:00 KST 장외 시간
    scheduler.add_job(
        auto_update_job,
        CronTrigger(hour=2, minute=0),
        args=[ctx],
        id="auto_update",
        max_instances=1,
        coalesce=True,
    )
    # paper 계좌 90일 만료 체크 — 매일 08:00 KST (장 시작 1시간 전)
    scheduler.add_job(
        paper_expiry_check_job,
        CronTrigger(hour=8, minute=0),
        args=[ctx],
        id="paper_expiry_check",
        max_instances=1,
        coalesce=True,
    )
    # credentials.env 파일 변경 감시 — 5분마다
    scheduler.add_job(
        credentials_watcher_job,
        CronTrigger(minute="*/5"),
        args=[ctx],
        id="credentials_watcher",
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

    mode_line = (
        "🔴 *실전 계좌* 로 동작 중이에요 — 실제 돈이 움직입니다."
        if settings.kis.mode == "live"
        else "🟡 *모의 계좌* 로 동작 중이에요."
    )
    if kill_switch.is_active():
        action_line = (
            f"⏰ 평일 09–15시, {settings.cycle_minutes}분마다 신호를 확인해요.\n"
            f"🛑 긴급 정지가 켜져 있어서 새로 구매는 안 하지만, 갖고 있는 주식의 "
            f"자동 판매(손절/익절)는 계속 동작해요."
        )
    else:
        action_line = (
            f"⏰ 평일 09–15시, {settings.cycle_minutes}분마다 신호를 확인하고 "
            f"조건이 맞으면 자동으로 거래해요."
        )
    telegram.send(
        settings.telegram,
        (
            "✅ *정상적으로 시작되었습니다!*\n\n"
            f"{mode_line}\n"
            f"{action_line}\n\n"
            "명령어: `/menu` 또는 `/help`"
        ),
    )
    log.info("Scheduler 시작 (매 %d분)", settings.cycle_minutes)
    scheduler.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
