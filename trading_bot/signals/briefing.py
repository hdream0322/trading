from __future__ import annotations

import logging
from datetime import datetime

from trading_bot.bot.commands import decision_ko, fmt_pct, fmt_won, mode_badge
from trading_bot.config import Settings
from trading_bot.kis.client import KisClient
from trading_bot.notify import telegram
from trading_bot.risk import kill_switch
from trading_bot.store import repo

log = logging.getLogger(__name__)


def send_open_briefing(settings: Settings, kis: KisClient) -> None:
    """장 시작 브리핑 — 평일 09:00 KST.

    조용 모드(/quiet) 와 무관하게 매일 전송. 잔고 / 킬스위치 / 보유종목 요약.
    """
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

    # 전일 실적 조회
    recent_pnl = repo.get_recent_pnl_daily(days=2)
    yesterday_pnl = recent_pnl[0] if recent_pnl else None

    lines = [
        f"🌅 *장 시작 브리핑* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총 자산 {fmt_won(balance_summary.get('tot_evlu_amt'))} · "
        f"현금 {fmt_won(balance_summary.get('dnca_tot_amt'))}",
        f"보유 {len(holdings)}종목 / 추적 {len(settings.universe)}종목 / "
        f"{settings.cycle_minutes}분마다 점검",
    ]

    # 전일 실적 한 줄 요약
    if yesterday_pnl and yesterday_pnl.get("ending_equity"):
        y_trades = yesterday_pnl.get("trade_count", 0)
        y_unreal = yesterday_pnl.get("unrealized_pnl")
        y_line = f"어제({yesterday_pnl['date']}) 거래 {y_trades}건"
        if y_unreal is not None:
            sign = "+" if y_unreal >= 0 else ""
            y_line += f" · 평가손익 {sign}{int(y_unreal):,}원"
        lines.append(y_line)

    if kill_switch.is_active():
        lines.append("🛑 긴급 정지 상태 — 새 구매 차단 중 (자동 판매는 동작)")
    else:
        lines.append("✅ 오늘도 자동으로 신호를 확인할게요.")

    if holdings:
        lines.append("")
        lines.append(f"*갖고 있는 주식 {len(holdings)}개*")
        for code, pos in list(holdings.items())[:10]:
            name = pos.get("name", "")
            qty = int(pos.get("qty", 0))
            cur_price = int(pos.get("cur_price", 0))
            pnl_pct = pos.get("evlu_pfls_rt")
            pnl_emoji = "🟢" if (pnl_pct is not None and float(pnl_pct) >= 0) else "🔴"
            lines.append("")
            lines.append(f"{pnl_emoji} *{name}* (`{code}`)")
            lines.append(f"   {qty}주 · 지금 {cur_price:,}원 · {fmt_pct(pnl_pct)}")
        if len(holdings) > 10:
            lines.append("")
            lines.append(f"_…외 {len(holdings) - 10}개 (전체는 /positions)_")

    telegram.send(settings.telegram, "\n".join(lines))


def send_close_briefing(settings: Settings, kis: KisClient) -> None:
    """장 마감 브리핑 — 평일 15:35 KST.

    조용 모드와 무관하게 매일 전송.
    오늘 처리된 주문 내역 + 누적 LLM 비용 + 잔고 요약 + 사후 설명.
    부가로 pnl_daily 에 오늘 실적 레코드 기록.
    """
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
    monthly_cost = repo.monthly_llm_cost_usd()
    signal_summary = repo.get_today_signal_summary()
    risk_reasons = repo.get_today_risk_rejection_reasons()
    recent_pnl = repo.get_recent_pnl_daily(days=5)

    submitted = [o for o in orders if o.get("status") == "submitted"]
    rejected = [o for o in orders if o.get("status") == "rejected"]
    errored = [o for o in orders if o.get("status") == "error"]

    # 오늘 실적을 pnl_daily 에 기록 (date 중복이면 덮어씀).
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        ending_equity = float(balance_summary.get("tot_evlu_amt") or 0)
        unrealized = float(balance_summary.get("evlu_pfls_smtl_amt") or 0)
        trade_count = len(submitted)
        repo.upsert_pnl_daily(
            date=today_str,
            starting_equity=None,
            ending_equity=ending_equity,
            realized_pnl=None,
            unrealized_pnl=unrealized,
            trade_count=trade_count,
        )
    except Exception:
        log.exception("pnl_daily 기록 실패 (브리핑은 계속 전송)")

    lines = [
        f"🌇 *장 마감 브리핑* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총 자산 {fmt_won(balance_summary.get('tot_evlu_amt'))} · "
        f"현금 {fmt_won(balance_summary.get('dnca_tot_amt'))} · "
        f"어제 대비 {fmt_pct(balance_summary.get('asst_icdc_erng_rt'))}",
        f"오늘 주문 접수 {len(submitted)}건 · 안전장치 차단 {len(rejected)}건 · "
        f"에러 {len(errored)}건",
        f"오늘 AI 비용 ${daily_cost:.4f} · 이번 달 ${monthly_cost:.4f}",
    ]

    # 주간 수익률 (최근 5거래일 ending_equity 변동)
    if len(recent_pnl) >= 2:
        oldest = recent_pnl[-1]
        newest = recent_pnl[0]
        oldest_eq = oldest.get("ending_equity")
        newest_eq = newest.get("ending_equity")
        if oldest_eq and newest_eq and oldest_eq > 0:
            week_return = (newest_eq - oldest_eq) / oldest_eq * 100
            week_trades = sum(p.get("trade_count", 0) for p in recent_pnl)
            sign = "+" if week_return >= 0 else ""
            lines.append(
                f"최근 {len(recent_pnl)}거래일 수익률 {sign}{week_return:.2f}% · "
                f"거래 {week_trades}건"
            )

    if submitted:
        lines.append("")
        lines.append("*✅ 접수된 주문*")
        for o in submitted:
            side_ko = decision_ko(str(o.get("side", "")))
            lines.append(
                f"• {o.get('name') or ''} ({o.get('code')}) {side_ko} {o.get('qty')}주"
            )

    # 사후 설명: "왜 오늘 거래가 없었나"
    # 거래가 없었고(접수 0) 사이클은 돌아간 경우(total_checks > 0)에만 표시.
    if not submitted and signal_summary["total_checks"] > 0:
        lines.append("")
        lines.append("*📋 오늘 왜 거래가 없었나요?*")
        lines.append(
            f"- 총 {signal_summary['total_checks']}회 점검 / "
            f"1차 통과 {signal_summary['prefilter_pass']}개 후보"
        )
        if signal_summary["prefilter_pass"] == 0:
            lines.append(
                "- 1차 조건(RSI/거래량) 통과한 종목이 없어서 AI 판단까지 안 갔어요."
            )
        else:
            ai_line = (
                f"- AI 판단: 구매 {signal_summary['llm_buy']} · "
                f"판매 {signal_summary['llm_sell']} · 관망 {signal_summary['llm_hold']}"
            )
            lines.append(ai_line)
            if signal_summary["low_confidence"] > 0:
                lines.append(
                    f"- 확신도 75% 미달로 주문까지 못 간 건 {signal_summary['low_confidence']}건"
                )
        if risk_reasons:
            reasons_str = ", ".join(f"{k} {v}건" for k, v in risk_reasons)
            lines.append(f"- 안전장치가 막은 사유: {reasons_str}")

    if not submitted and not rejected and not errored and signal_summary["total_checks"] == 0:
        lines.append("")
        lines.append("_오늘은 사이클이 돌지 않았어요. (휴장일일 수 있음)_")

    telegram.send(settings.telegram, "\n".join(lines))
