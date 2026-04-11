from __future__ import annotations

import logging
from datetime import datetime

from trading_bot.bot import quiet_mode
from trading_bot.bot.commands import decision_ko, fmt_pct, fmt_won, mode_badge
from trading_bot.config import Settings
from trading_bot.kis.client import KisClient
from trading_bot.notify import telegram
from trading_bot.risk import kill_switch
from trading_bot.store import repo

log = logging.getLogger(__name__)


def send_open_briefing(settings: Settings, kis: KisClient) -> None:
    """장 시작 브리핑 — 평일 09:00 KST.

    조용 모드(data/QUIET_MODE) 활성 시 전송 생략.
    잔고 / 킬스위치 / 보유종목 요약을 보낸다.
    """
    if quiet_mode.is_active():
        log.info("조용 모드 — 장 시작 브리핑 스킵")
        return

    badge = mode_badge(settings.kis.mode)
    try:
        balance = kis.get_balance()
        balance_summary = balance.get("summary", {}) or {}
        holdings = KisClient.normalize_holdings(balance.get("holdings", []))
    except Exception as exc:
        log.exception("장 시작 브리핑 잔고 조회 실패")
        telegram.send(
            settings.telegram,
            f"🌅 *장 시작 브리핑* {badge}\n계좌 조회 실패 — `{exc}`",
        )
        return

    lines = [
        f"🌅 *장 시작 브리핑* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총 자산 {fmt_won(balance_summary.get('tot_evlu_amt'))} · "
        f"현금 {fmt_won(balance_summary.get('dnca_tot_amt'))}",
        f"보유 {len(holdings)}종목 / 추적 {len(settings.universe)}종목 / "
        f"{settings.cycle_minutes}분마다 점검",
    ]
    if kill_switch.is_active():
        lines.append("🛑 긴급 정지 상태 — 새 구매 차단 중 (자동 판매는 동작)")
    else:
        lines.append("✅ 오늘도 자동으로 신호를 확인할게요.")

    if holdings:
        lines.append("")
        lines.append("*갖고 있는 주식*")
        for code, pos in list(holdings.items())[:10]:
            name = pos.get("name", "")
            qty = int(pos.get("qty", 0))
            cur_price = int(pos.get("cur_price", 0))
            pnl_pct = pos.get("evlu_pfls_rt")
            lines.append(
                f"• {name} ({code}) {qty}주 @ {cur_price:,}원 · {fmt_pct(pnl_pct)}"
            )

    telegram.send(settings.telegram, "\n".join(lines))


def send_close_briefing(settings: Settings, kis: KisClient) -> None:
    """장 마감 브리핑 — 평일 15:35 KST.

    조용 모드 활성 시 전송 생략.
    오늘 처리된 주문 내역 + 누적 LLM 비용 + 잔고 요약.
    """
    if quiet_mode.is_active():
        log.info("조용 모드 — 장 마감 브리핑 스킵")
        return

    badge = mode_badge(settings.kis.mode)
    try:
        balance = kis.get_balance()
        balance_summary = balance.get("summary", {}) or {}
    except Exception as exc:
        log.exception("장 마감 브리핑 잔고 조회 실패")
        telegram.send(
            settings.telegram,
            f"🌇 *장 마감 브리핑* {badge}\n계좌 조회 실패 — `{exc}`",
        )
        return

    orders = repo.get_today_orders()
    daily_cost = repo.today_llm_cost_usd()

    submitted = [o for o in orders if o.get("status") == "submitted"]
    rejected = [o for o in orders if o.get("status") == "rejected"]
    errored = [o for o in orders if o.get("status") == "error"]

    lines = [
        f"🌇 *장 마감 브리핑* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총 자산 {fmt_won(balance_summary.get('tot_evlu_amt'))} · "
        f"현금 {fmt_won(balance_summary.get('dnca_tot_amt'))} · "
        f"어제 대비 {fmt_pct(balance_summary.get('asst_icdc_erng_rt'))}",
        f"오늘 주문 접수 {len(submitted)}건 · 안전장치 차단 {len(rejected)}건 · "
        f"에러 {len(errored)}건",
        f"오늘 AI 비용 ${daily_cost:.4f}",
    ]

    if submitted:
        lines.append("")
        lines.append("*✅ 접수된 주문*")
        for o in submitted:
            side_ko = decision_ko(str(o.get("side", "")))
            lines.append(
                f"• {o.get('name') or ''} ({o.get('code')}) {side_ko} {o.get('qty')}주"
            )

    if not submitted and not rejected and not errored:
        lines.append("")
        lines.append("_오늘은 거래 이벤트가 없었어요._")

    telegram.send(settings.telegram, "\n".join(lines))
