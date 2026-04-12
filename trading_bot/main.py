from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime, timedelta
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
from trading_bot.signals.briefing import send_close_briefing, send_open_briefing
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
    """매일 08:30 KST — 자동 업데이트 스케줄 훅.

    한국·미국 장 모두 닫힌 시간대. 상태 파일(data/AUTO_UPDATE_DISABLED)
    검사 후 활성 상태면 GHCR digest 사전 확인 → 새 이미지가 있을 때만
    Watchtower HTTP API 호출. 결과를 텔레그램으로 알려준다.
    """
    from trading_bot import __version__ as current_version
    from trading_bot.bot.commands_update import _summarize_release_body

    if not update_manager.is_auto_enabled():
        log.info("자동 업데이트 비활성 상태 — 02:00 스케줄 스킵")
        return

    # 사전 확인: 새 이미지가 있는지 digest 비교
    has_update = True  # 확인 실패 시 보수적으로 업데이트 시도
    try:
        has_update, _cur, _rem = update_manager.check_for_update()
    except Exception:
        log.warning("업데이트 사전 확인 실패 — 보수적으로 업데이트 시도", exc_info=True)

    if not has_update:
        log.info("이미 최신 이미지 — 08:30 자동 업데이트 스킵")
        return

    # 릴리스 정보 조회 (실패해도 업데이트는 진행)
    info: dict[str, str] | None = None
    try:
        info = update_manager.fetch_latest_release_info()
    except Exception:
        log.debug("릴리스 정보 조회 실패 — 알림에서 버전 생략")

    try:
        result = update_manager.trigger_update(ctx.settings.watchtower_http_token)
        log.info("자동 업데이트 요청 전송 완료: %s", result.get("status"))

        latest_version = (info or {}).get("tag") or "?"
        lines = [
            "🔄 *새 버전 발견, 업데이트 시작*",
            "",
            f"📦 `{current_version}` → `{latest_version}`",
            "⏳ 약 30~60초 후 자동으로 재시작됩니다.",
        ]
        summary = _summarize_release_body((info or {}).get("body") or "")
        if summary:
            lines.append("")
            lines.append("📋 *이번 변경 사항*")
            lines.append("```")
            lines.append(summary)
            lines.append("```")

        try:
            telegram.send(ctx.settings.telegram, "\n".join(lines))
        except Exception:
            log.exception("자동 업데이트 알림 전송 실패")
    except Exception as exc:
        log.exception("자동 업데이트 요청 실패")
        try:
            telegram.send(
                ctx.settings.telegram,
                f"❌ *자동 업데이트 실패*\n"
                f"08:30 자동 업데이트를 시도했지만 실패했어요.\n"
                f"`{exc}`\n\n"
                f"수동 재시도: `/update confirm`",
            )
        except Exception:
            log.exception("자동 업데이트 실패 알림 전송 실패")


def weekly_holiday_reminder_job(ctx: BotContext) -> None:
    """매주 월요일 07:00 KST — 휴장일 YAML 점검 리마인더.

    임시공휴일이 중간에 지정될 수 있어서 주간 확인이 필요. YAML 을 다시 로드한
    뒤 앞으로 14일 이내 등록된 휴장일을 보여주고, KRX 포털 링크를 함께 보냄.
    사용자가 직접 확인·수정하는 흐름.
    """
    from trading_bot.utils.calendar_kr import reload_holidays, upcoming_holidays

    try:
        count = reload_holidays()
        upcoming = upcoming_holidays(days_ahead=14)
    except Exception:
        log.exception("주간 휴장일 리마인더 실패")
        return

    lines = [
        "📅 *주간 휴장일 점검*",
        f"등록된 휴장일 총 {count}개 로드 완료.",
        "",
    ]
    if upcoming:
        lines.append("*앞으로 14일 이내 등록된 휴장일*")
        for d in upcoming:
            weekday_ko = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
            lines.append(f"- {d:%Y-%m-%d} ({weekday_ko})")
    else:
        lines.append("_앞으로 14일 내 등록된 휴장일 없음._")

    lines.append("")
    lines.append(
        "⚠️ *임시공휴일이 갑자기 지정될 수 있어요.*\n"
        "KRX 공식 캘린더와 비교해 확인해주세요:\n"
        "https://open.krx.co.kr/contents/MKD/01/0110/01100305/MKD01100305.jsp\n\n"
        "수정이 필요하면 `config/market_holidays.yaml` 파일을 고치고 "
        "`/restart` 로 봇을 재시작하면 반영됩니다."
    )

    try:
        telegram.send(ctx.settings.telegram, "\n".join(lines))
    except Exception:
        log.exception("주간 휴장일 리마인더 전송 실패")


def fundamentals_refresh_job(ctx: BotContext) -> None:
    """매주 일요일 03:00 KST — 유니버스 전체 재무지표 캐시 갱신.

    장외 시간 배치 실행으로 KIS throttle 부담 없음.
    fundamentals.enabled=false 이면 스킵.
    """
    funda_cfg = getattr(ctx.settings, "fundamentals", None) or {}
    from trading_bot.bot.commands_funda import is_enabled as _funda_is_enabled
    if not _funda_is_enabled(funda_cfg):
        log.info("펀더멘털 갱신 비활성 — 스킵")
        return
    try:
        from trading_bot.signals import fundamentals
        result = fundamentals.refresh_universe(ctx.settings.universe, ctx.kis)
        log.info("펀더멘털 갱신 완료: %s", result)
        if result.get("failed", 0) > 0:
            telegram.send(
                ctx.settings.telegram,
                f"📊 *펀더멘털 주간 갱신*\n"
                f"성공 {result['success']}개 · 실패 {result['failed']}개",
            )
    except Exception:
        log.exception("펀더멘털 갱신 잡 실패")


# 자동 킬스위치 복구 정책
# - 자동 활성화 후 최소 15분 경과 + 최근 30분 에러 0건 → 자동 해제
# - 해제 직후 1시간 내 재활성화되면 그 뒤로는 수동 해제만 허용 (플래핑 방지)
_AUTO_KILL_THRESHOLD = 10          # 최근 1시간 에러 이만큼 쌓이면 자동 활성화
_AUTO_KILL_MIN_ACTIVE_MIN = 15     # 활성화 후 최소 이 시간은 유지 (즉시 풀림 방지)
_AUTO_KILL_RECOVERY_WINDOW_MIN = 30  # 이 시간 동안 에러 0건이면 복구로 판정
_AUTO_KILL_FLAP_WINDOW_H = 1       # 최근 이 시간 내 자동 해제 이력이 있으면 재활성 후 수동만


def error_spike_watchdog_job(ctx: BotContext) -> None:
    """5분마다 — 회로차단기 겸 자동 복구 체크.

    사일런트 실패(예: 토큰 만료, API 변경, 네트워크 장애) 를 감지해
    사용자에게 긴급 알림 + 신규 구매 자동 차단.

    동작:
    - 활성화: 최근 1시간 에러 >= 10건, 킬스위치 꺼져 있을 때 → 자동 활성화 + 알림
    - 자동 해제: 자동 활성화된 상태 + 최소 15분 경과 + 최근 30분 에러 0건 → 자동 해제 + 알림
    - 플래핑 방지: 최근 1시간 내 자동 해제 이력 있으면 재활성화는 그대로 하되 자동 해제는 안 함
    - 수동(/stop, touch) 으로 걸린 킬스위치는 절대 자동 해제하지 않음
    """
    from trading_bot.risk import kill_switch
    from trading_bot.store import repo

    try:
        recent_1h = repo.count_recent_errors(minutes=60)
    except Exception:
        log.exception("에러 카운트 조회 실패")
        return

    # ─── 복구 경로 — 이미 자동 활성화된 상태라면 해제 조건 검사 ───
    if kill_switch.is_active() and kill_switch.is_auto_triggered():
        activated_at = kill_switch.get_activated_at()
        if activated_at is None:
            return  # 파일 파싱 실패 — 안전하게 그대로 둠

        elapsed = datetime.now() - activated_at
        if elapsed < timedelta(minutes=_AUTO_KILL_MIN_ACTIVE_MIN):
            return  # 최소 유지 시간 미달

        try:
            recent_30m = repo.count_recent_errors(minutes=_AUTO_KILL_RECOVERY_WINDOW_MIN)
        except Exception:
            log.exception("복구 판정용 에러 카운트 조회 실패")
            return
        if recent_30m > 0:
            return  # 복구 창 안에 에러 있음 — 아직 정상 아님

        # 복구 조건 충족 → 자동 해제
        kill_switch.deactivate(auto=True)
        log.critical(
            "자동 킬스위치 복구: 활성화 %d분 경과, 최근 %d분 에러 0건",
            int(elapsed.total_seconds() / 60),
            _AUTO_KILL_RECOVERY_WINDOW_MIN,
        )
        try:
            telegram.send(
                ctx.settings.telegram,
                (
                    "✅ *자동 복구 — 비상정지 풀림*\n\n"
                    f"자동으로 걸렸던 비상정지가 *풀렸어요*.\n"
                    f"({_AUTO_KILL_RECOVERY_WINDOW_MIN}분 동안 에러가 0건이라 정상 복구된 걸로 판단)\n\n"
                    "다음 점검부터 새로 구매가 다시 가능합니다.\n"
                    "(같은 문제가 1시간 안에 또 생기면 그땐 직접 `/resume` 해주셔야 해요.)"
                ),
            )
        except Exception:
            log.exception("자동 복구 알림 전송 실패")
        return

    # ─── 활성화 경로 — 에러 급증 감지 ───
    if recent_1h < _AUTO_KILL_THRESHOLD:
        return
    if kill_switch.is_active():
        # 수동으로 걸린 상태거나 이미 자동 활성 상태 — 추가 알림/재활성 안 함
        return

    log.critical("에러 급증 감지: 최근 1시간 %d건 → 자동 킬스위치 활성화", recent_1h)
    kill_switch.activate(
        reason=f"에러 급증 자동 차단 (최근 1시간 {recent_1h}건)",
        auto=True,
    )

    # 최근 1시간 내 자동 해제 이력이 있으면 = 이미 한 번 풀렸다 또 걸린 상태
    # → 이 알림에 "이번엔 수동 해제 필요" 경고를 추가
    flapped = kill_switch.count_recent_auto_releases(hours=_AUTO_KILL_FLAP_WINDOW_H) > 0

    try:
        if flapped:
            telegram.send(
                ctx.settings.telegram,
                (
                    "🚨 *긴급 — 에러 급증 재발 (플래핑)*\n\n"
                    f"조금 전 자동 복구했는데 다시 에러가 *{recent_1h}건* 쌓였어요.\n"
                    "구조적 문제일 수 있어서 **이번엔 자동으로 안 풀립니다**.\n\n"
                    "확인 방법:\n"
                    "- `/status` 로 현재 상태 점검\n"
                    "- `/signals` 로 오늘 사이클 결과 확인\n"
                    "- NAS 로그에서 원인 파악\n\n"
                    "조치 후 `/resume` 으로 직접 풀어주세요."
                ),
            )
        else:
            telegram.send(
                ctx.settings.telegram,
                (
                    "🚨 *긴급 — 에러 급증 감지*\n\n"
                    f"최근 1시간 동안 에러가 *{recent_1h}건* 쌓였어요.\n"
                    "뭔가 잘못 돌고 있을 수 있어서 **신규 구매를 자동으로 차단**했습니다.\n"
                    "(갖고 있는 주식의 자동 판매는 계속 작동합니다.)\n\n"
                    f"_{_AUTO_KILL_RECOVERY_WINDOW_MIN}분 동안 에러가 없으면 알아서 다시 풀려요._\n"
                    "_급하면 `/status` 로 상태 확인 후 `/resume` 으로 직접 풀 수도 있어요._"
                ),
            )
    except Exception:
        log.exception("에러 급증 알림 전송 실패")


def db_backup_job(ctx: BotContext) -> None:
    """매일 02:00 KST — SQLite 스냅샷 백업 + 7일 초과분 제거.

    auto_update_job 과 같은 시각이지만 순서 영향 없음 (둘 다 독립).
    """
    from trading_bot.store import backup
    try:
        path = backup.create_daily_backup()
        removed = backup.prune_old_backups()
        log.info("DB 백업 스케줄 완료: %s, 정리 %d개", path, removed)
    except Exception:
        log.exception("DB 백업 잡 실패")


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


def open_briefing_job(ctx: BotContext) -> None:
    """평일 09:00 KST — 장 시작 브리핑.

    조용 모드면 quiet_mode 측에서 스킵. 휴장일이면 전송 생략.
    """
    from trading_bot.utils.calendar_kr import is_trading_day
    if not is_trading_day(datetime.now().date()):
        log.info("휴장일 — 장 시작 브리핑 스킵")
        return
    try:
        send_open_briefing(ctx.settings, ctx.kis)
    except Exception:
        log.exception("장 시작 브리핑 실패")


def close_briefing_job(ctx: BotContext) -> None:
    """평일 15:35 KST — 장 마감 브리핑."""
    from trading_bot.utils.calendar_kr import is_trading_day
    if not is_trading_day(datetime.now().date()):
        log.info("휴장일 — 장 마감 브리핑 스킵")
        return
    try:
        send_close_briefing(ctx.settings, ctx.kis)
    except Exception:
        log.exception("장 마감 브리핑 실패")


def accuracy_eval_job(ctx: BotContext) -> None:
    """평일 16:30 KST — 사후 정확도 평가.

    signals 테이블에서 5 거래일 경과한 buy/sell 판단을 꺼내
    해당 종목의 5 거래일 뒤 종가 기준 forward return 을 DB 에 기록.
    `/accuracy` 커맨드로 confidence 구간별 집계 확인 가능.
    """
    from trading_bot.utils.calendar_kr import is_trading_day
    if not is_trading_day(datetime.now().date()):
        return
    try:
        from trading_bot.signals import accuracy
        result = accuracy.evaluate_pending_signals(ctx.kis)
        if result["evaluated"] > 0:
            log.info("사후 정확도 평가 완료: %s", result)
    except Exception:
        log.exception("사후 정확도 평가 실패")


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
        new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
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
    kis = KisClient.from_settings(settings)
    risk = RiskManager(settings)
    ctx = BotContext(settings=settings, kis=kis, risk=risk, llm=llm)

    # universe 섹터 자동 백필 — settings.yaml 에서 처음 올라온 종목은 sector 가
    # 비어있으니 KIS inquire-price 로 업종명을 가져와 universe.json 에 저장.
    # 섹터 분산 리스크 게이트가 참조. 실패해도 기동은 계속 (sector 없으면 우회).
    try:
        from trading_bot.bot.universe_helper import backfill_sectors
        backfill_sectors(settings.universe, kis)
    except Exception:
        log.exception("universe 섹터 백필 실패 (기동은 계속)")

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
    # 자동 업데이트 — 매일 08:30 KST (한국·미국 장 모두 닫힌 시간)
    scheduler.add_job(
        auto_update_job,
        CronTrigger(hour=8, minute=30),
        args=[ctx],
        id="auto_update",
        max_instances=1,
        coalesce=True,
    )
    # DB 백업 — 매일 01:55 KST (자동 업데이트 직전, 재시작 영향 회피)
    scheduler.add_job(
        db_backup_job,
        CronTrigger(hour=1, minute=55),
        args=[ctx],
        id="db_backup",
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
    # 에러 급증 회로차단기 — 5분마다 최근 1시간 에러 건수 체크
    scheduler.add_job(
        error_spike_watchdog_job,
        CronTrigger(minute="*/5"),
        args=[ctx],
        id="error_spike_watchdog",
        max_instances=1,
        coalesce=True,
    )
    # 장 시작 브리핑 — 평일 09:00 KST
    scheduler.add_job(
        open_briefing_job,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0),
        args=[ctx],
        id="open_briefing",
        max_instances=1,
        coalesce=True,
    )
    # 장 마감 브리핑 — 평일 15:35 KST (마지막 사이클 15:30 과 겹치지 않게)
    scheduler.add_job(
        close_briefing_job,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
        args=[ctx],
        id="close_briefing",
        max_instances=1,
        coalesce=True,
    )
    # 사후 정확도 평가 — 평일 16:30 KST (장 마감 브리핑 이후)
    scheduler.add_job(
        accuracy_eval_job,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30),
        args=[ctx],
        id="accuracy_eval",
        max_instances=1,
        coalesce=True,
    )
    # 주간 휴장일 점검 리마인더 — 매주 월요일 07:00 KST
    scheduler.add_job(
        weekly_holiday_reminder_job,
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        args=[ctx],
        id="weekly_holiday_reminder",
        max_instances=1,
        coalesce=True,
    )
    # 펀더멘털 주간 갱신 — 매주 일요일 03:00 KST (장외 시간 배치)
    scheduler.add_job(
        fundamentals_refresh_job,
        CronTrigger(day_of_week="sun", hour=3, minute=0),
        args=[ctx],
        id="fundamentals_refresh",
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
