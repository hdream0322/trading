from __future__ import annotations

import json
import logging
import traceback as tb_module
from datetime import datetime
from typing import Any

from trading_bot.bot.commands import (
    confidence_pct,
    cycle_summary_keyboard,
    decision_ko,
    fmt_pct,
    fmt_won,
    mode_badge,
)
from trading_bot.config import Settings
from trading_bot.kis.client import KisClient
from trading_bot.notify import telegram
from trading_bot.risk import kill_switch
from trading_bot.risk.manager import RiskDecision, RiskManager
from trading_bot.signals import exit_strategy, indicators, prefilter
from trading_bot.signals.llm import ClaudeSignalClient, LlmDecision
from trading_bot.store import repo

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_cycle(
    settings: Settings,
    kis: KisClient,
    llm: ClaudeSignalClient | None,
    risk: RiskManager,
) -> dict[str, Any]:
    """단일 시그널 사이클. 시그널 발효 시 리스크 게이트 통과하면 실제 주문까지 실행."""
    log.info("=== 사이클 시작 (mode=%s) ===", settings.kis.mode)
    summary: dict[str, Any] = {
        "total": 0, "candidates": 0,
        "buy": 0, "sell": 0, "hold": 0, "errors": 0,
        "cost_usd": 0.0,
        "orders_submitted": 0, "orders_rejected_by_risk": 0,
        "exits_executed": 0,
    }

    daily_cost = repo.today_llm_cost_usd()
    daily_limit = float(settings.llm.get("daily_cost_limit_usd", 5.0))
    log.info("오늘 LLM 누적 비용: $%.4f / 한도 $%.2f", daily_cost, daily_limit)
    log.info("킬스위치: %s", "활성" if kill_switch.is_active() else "비활성")

    # 1. 잔고 스냅샷 (사이클 시작 시 1회)
    try:
        balance = kis.get_balance()
        balance_summary = balance.get("summary", {}) or {}
        holdings = KisClient.normalize_holdings(balance.get("holdings", []))
    except Exception as exc:
        log.exception("잔고 조회 실패 — 사이클 중단")
        telegram.send(
            settings.telegram,
            f"🚨 계좌 조회 실패로 이번 점검 중단\n`{exc}`",
        )
        return summary

    log.info(
        "잔고: 총평가 %s원 / 예수금 %s원 / 보유 %d종목 / 전일대비 %s%%",
        balance_summary.get("tot_evlu_amt", "?"),
        balance_summary.get("dnca_tot_amt", "?"),
        len(holdings),
        balance_summary.get("asst_icdc_erng_rt", "?"),
    )

    # ─────────────────────────────────────────────
    # 0. 청산 체크 (손절/익절/트레일링) — 유니버스 스캔보다 먼저
    # ─────────────────────────────────────────────
    exit_events: list[dict[str, Any]] = []
    exit_cfg = getattr(settings, "exit_rules", None) or {}
    if exit_cfg and holdings:
        exit_events = _run_exit_checks(
            settings, kis, risk, holdings, balance_summary, exit_cfg
        )
        summary["exits_executed"] = len(exit_events)
        # 청산된 종목은 유니버스 스캔에서 제외 (이미 판매됨 또는 판매 중)
        for ev in exit_events:
            holdings.pop(ev["code"], None)

    threshold = float(settings.llm.get("confidence_threshold", 0.75))
    executed_events: list[dict[str, Any]] = list(exit_events)

    for item in settings.universe:
        code = str(item["code"])
        name = str(item["name"])
        summary["total"] += 1

        try:
            ohlcv = kis.get_daily_ohlcv(code, days=30)
            if len(ohlcv) < 20:
                log.warning("%s %s: 캔들 부족 (%d개)", code, name, len(ohlcv))
                summary["errors"] += 1
                continue

            closes = [c["close"] for c in ohlcv]
            volumes = [c["volume"] for c in ohlcv]

            rsi_period = int(settings.prefilter.get("rsi_period", 14))
            rsi_val = indicators.rsi(closes, period=rsi_period)
            vol_ratio = indicators.volume_ratio(volumes, lookback=20)
            current_price = closes[-1]
            prev_close = closes[-2] if len(closes) >= 2 else current_price
            change_pct = ((current_price - prev_close) / prev_close * 100.0) if prev_close else 0.0

            features: dict[str, Any] = {
                "code": code,
                "name": name,
                "current_price": current_price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "rsi": rsi_val,
                "volume_ratio": vol_ratio,
            }

            candidate = prefilter.evaluate(features, settings.prefilter)

            if candidate is None:
                repo.insert_signal(
                    ts=_now_iso(),
                    code=code, name=name,
                    decision="hold", confidence=None,
                    rule_features=json.dumps(features, ensure_ascii=False),
                    llm_model=None,
                    llm_reasoning="1차 조건 통과 못함 (RSI/거래량 기준 미달)",
                    llm_input_tokens=None, llm_output_tokens=None,
                    llm_cost_usd=None,
                )
                summary["hold"] += 1
                log.info("%s %s: 관망 (RSI=%.1f, vol=%.2fx)", code, name, rsi_val, vol_ratio)
                continue

            summary["candidates"] += 1

            # LLM 경로
            if llm is None:
                repo.insert_signal(
                    ts=_now_iso(),
                    code=code, name=name,
                    decision=candidate.side_hint, confidence=None,
                    rule_features=json.dumps(features, ensure_ascii=False),
                    llm_model=None,
                    llm_reasoning="AI 비활성 — 1차 조건만 통과 기록",
                    llm_input_tokens=None, llm_output_tokens=None,
                    llm_cost_usd=None,
                )
                summary[candidate.side_hint] += 1
                log.info("%s %s → %s (AI 비활성 — 주문 실행 안 함)", code, name, candidate.side_hint)
                continue

            if daily_cost >= daily_limit:
                log.warning("일일 LLM 비용 한도($%.2f) 도달, %s 스킵", daily_limit, code)
                summary["errors"] += 1
                continue

            decision = llm.decide(features, candidate.side_hint, ohlcv)
            daily_cost += decision.cost_usd
            summary["cost_usd"] += decision.cost_usd

            repo.insert_signal(
                ts=_now_iso(),
                code=code, name=name,
                decision=decision.decision,
                confidence=decision.confidence,
                rule_features=json.dumps(features, ensure_ascii=False),
                llm_model=decision.model,
                llm_reasoning=decision.reasoning,
                llm_input_tokens=decision.input_tokens,
                llm_output_tokens=decision.output_tokens,
                llm_cost_usd=decision.cost_usd,
            )
            summary[decision.decision] += 1

            log.info(
                "%s %s → %s (conf=%.2f, cost=$%.4f) %s",
                code, name, decision.decision, decision.confidence,
                decision.cost_usd, decision.reasoning[:100],
            )

            # 시그널 발효 조건: buy/sell + confidence >= threshold
            if decision.decision == "hold" or decision.confidence < threshold:
                continue

            # 리스크 게이트
            rd: RiskDecision = risk.check(
                side=decision.decision,
                code=code,
                name=name,
                current_price=current_price,
                balance_summary=balance_summary,
                holdings=holdings,
            )

            if not rd.allowed:
                summary["orders_rejected_by_risk"] += 1
                log.warning(
                    "%s %s %s 시그널 리스크 차단: %s",
                    code, name, decision.decision, rd.reason,
                )
                repo.insert_order(
                    ts=_now_iso(),
                    code=code, name=name, side=decision.decision,
                    qty=0, price=None,
                    mode=settings.kis.mode,
                    kis_order_no=None,
                    status="rejected",
                    raw_response=None,
                    reason=rd.reason,
                )
                executed_events.append({
                    "type": "rejected",
                    "code": code, "name": name,
                    "side": decision.decision,
                    "reason": rd.reason,
                    "confidence": decision.confidence,
                })
                continue

            # 주문 실행
            log.info("%s %s %s %d주 시장가 주문 제출 중...", code, name, decision.decision, rd.qty)
            try:
                order_result = kis.place_market_order(code, decision.decision, rd.qty)
                order_no = order_result["order_no"]
                repo.insert_order(
                    ts=_now_iso(),
                    code=code, name=name, side=decision.decision,
                    qty=rd.qty, price=None,
                    mode=settings.kis.mode,
                    kis_order_no=order_no,
                    status="submitted",
                    raw_response=json.dumps(order_result["raw"], ensure_ascii=False)[:2000],
                    reason=f"conf={decision.confidence:.2f} / {rd.reason}",
                )
                summary["orders_submitted"] += 1
                log.info(
                    "✅ %s %s %s %d주 주문 제출 성공 (order_no=%s)",
                    code, name, decision.decision, rd.qty, order_no,
                )
                executed_events.append({
                    "type": "submitted",
                    "code": code, "name": name,
                    "side": decision.decision,
                    "qty": rd.qty,
                    "price": int(current_price),
                    "order_no": order_no,
                    "confidence": decision.confidence,
                    "reasoning": decision.reasoning,
                })
            except Exception as exc:
                log.exception("%s %s 주문 실행 실패", code, name)
                summary["errors"] += 1
                repo.insert_order(
                    ts=_now_iso(),
                    code=code, name=name, side=decision.decision,
                    qty=rd.qty, price=None,
                    mode=settings.kis.mode,
                    kis_order_no=None,
                    status="error",
                    raw_response=str(exc)[:500],
                    reason=f"exception: {type(exc).__name__}",
                )
                repo.insert_error(
                    component="order",
                    message=f"{code} {name} {decision.decision} {rd.qty}: {exc}",
                    traceback=tb_module.format_exc(),
                )

        except Exception as exc:
            log.exception("%s %s 처리 실패", code, name)
            summary["errors"] += 1
            repo.insert_error(
                component="cycle",
                message=f"{code} {name}: {exc}",
                traceback=tb_module.format_exc(),
            )

    _notify_summary(settings, summary, daily_cost, threshold, balance_summary, executed_events)
    log.info("=== 사이클 종료: %s ===", summary)
    return summary


def _run_exit_checks(
    settings: Settings,
    kis: KisClient,
    risk: RiskManager,
    holdings: dict[str, dict[str, Any]],
    balance_summary: dict[str, Any],
    exit_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """보유 포지션 각각에 대해 손절/익절/트레일링 스톱 체크 및 즉시 판매.

    반환: 실행된 청산 이벤트 목록 (텔레그램 요약 용도)
    """
    events: list[dict[str, Any]] = []

    # 1. position_state 동기화 — 신규/제거된 포지션 반영
    states = exit_strategy.sync_position_state(holdings, _now_iso())

    # 2. 각 포지션에 대해:
    #    a. high_water_mark 갱신 (+ trailing_active 전환)
    #    b. 청산 조건 검사
    #    c. 조건 충족 시 즉시 시장가 판매
    for code, pos in list(holdings.items()):
        state = states.get(code)
        if state is None:
            log.warning("%s: state 동기화 누락, 스킵", code)
            continue

        new_hwm, new_trailing = exit_strategy.update_high_water_mark(
            code, state, float(pos["cur_price"]), exit_cfg,
        )
        # state 로컬에도 반영 (check_exit 에서 참조)
        state["high_water_mark"] = new_hwm
        state["trailing_active"] = new_trailing

        decision = exit_strategy.check_exit(pos, state, exit_cfg)
        if not decision.should_exit:
            continue

        log.info(
            "청산 조건 충족 [%s] %s %s — %s",
            decision.tag, code, pos.get("name", ""), decision.reason,
        )

        # 리스크 게이트 (판매는 is_exit=True 로 daily order count 우회)
        rd = risk.check(
            side="sell",
            code=code,
            name=str(pos.get("name", "")),
            current_price=float(pos["cur_price"]),
            balance_summary=balance_summary,
            holdings=holdings,
            is_exit=True,
        )
        if not rd.allowed:
            log.warning("청산 차단 [%s] %s: %s", decision.tag, code, rd.reason)
            repo.insert_order(
                ts=_now_iso(),
                code=code, name=str(pos.get("name", "")), side="sell",
                qty=0, price=None,
                mode=settings.kis.mode,
                kis_order_no=None,
                status="rejected",
                raw_response=None,
                reason=f"exit blocked ({decision.tag}): {rd.reason}",
            )
            continue

        qty = int(pos["qty"])
        try:
            order_result = kis.place_market_order(code, "sell", qty)
            order_no = order_result["order_no"]
            repo.insert_order(
                ts=_now_iso(),
                code=code, name=str(pos.get("name", "")), side="sell",
                qty=qty, price=None,
                mode=settings.kis.mode,
                kis_order_no=order_no,
                status="submitted",
                raw_response=json.dumps(order_result["raw"], ensure_ascii=False)[:2000],
                reason=f"exit ({decision.tag}): {decision.reason}",
            )
            log.info(
                "✅ 청산 주문 접수 [%s] %s %d주 order_no=%s",
                decision.tag, code, qty, order_no,
            )
            events.append({
                "type": "exit",
                "tag": decision.tag,
                "code": code,
                "name": str(pos.get("name", "")),
                "qty": qty,
                "entry_price": decision.entry_price,
                "exit_price": decision.current_price,
                "pnl_pct": decision.pnl_pct,
                "reason": decision.reason,
                "order_no": order_no,
            })
        except Exception as exc:
            log.exception("청산 주문 실행 실패 %s", code)
            repo.insert_order(
                ts=_now_iso(),
                code=code, name=str(pos.get("name", "")), side="sell",
                qty=qty, price=None,
                mode=settings.kis.mode,
                kis_order_no=None,
                status="error",
                raw_response=str(exc)[:500],
                reason=f"exit error ({decision.tag}): {type(exc).__name__}",
            )

    return events


def _notify_summary(
    settings: Settings,
    summary: dict[str, Any],
    daily_cost: float,
    threshold: float,
    balance_summary: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    badge = mode_badge(settings.kis.mode)
    lines = [
        f"*점검 결과* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총 자산 {fmt_won(balance_summary.get('tot_evlu_amt'))} · "
        f"현금 {fmt_won(balance_summary.get('dnca_tot_amt'))} · "
        f"어제 대비 {fmt_pct(balance_summary.get('asst_icdc_erng_rt'))}",
        f"점검 종목 {summary['total']}개 / 후보 {summary['candidates']}개 / 오류 {summary['errors']}개",
        f"판단: 구매 {summary['buy']} · 판매 {summary['sell']} · 관망 {summary['hold']}",
        f"주문: 접수 {summary['orders_submitted']} · 안전장치 차단 {summary['orders_rejected_by_risk']}",
        f"자동 청산: {summary.get('exits_executed', 0)}건",
        f"AI 비용(이번 점검) ${summary['cost_usd']:.4f} · 오늘 누적 ${daily_cost:.4f}",
    ]

    exits = [e for e in events if e.get("type") == "exit"]
    submitted = [e for e in events if e.get("type") == "submitted"]
    rejected = [e for e in events if e.get("type") == "rejected"]

    if exits:
        lines.append("")
        lines.append("*💸 자동 청산 판매*")
        tag_emoji = {
            "stop_loss": "🛡️",
            "take_profit": "🎯",
            "trailing_stop": "📉",
        }
        for e in exits:
            emoji = tag_emoji.get(e["tag"], "•")
            lines.append(
                f"{emoji} {e['name']} ({e['code']}) {e['qty']}주\n"
                f"  구매가 {int(e['entry_price']):,}원 → 판매가 {int(e['exit_price']):,}원 "
                f"({e['pnl_pct']:+.2f}%)"
            )
            lines.append(f"  주문번호 `{e['order_no']}`")

    if submitted:
        lines.append("")
        lines.append("*✅ 신규 주문 접수*")
        for e in submitted:
            side_ko = decision_ko(e["side"])
            side_emoji = "🟢" if e["side"] == "buy" else "🔴"
            conf_str = confidence_pct(e["confidence"])
            lines.append(
                f"{side_emoji} {e['name']} ({e['code']}) {side_ko} "
                f"{e['qty']}주 @ 약 {int(e['price']):,}원 · 확신도 {conf_str}"
            )
            lines.append(f"  주문번호 `{e['order_no']}`")
            lines.append(f"  _{e['reasoning'][:140]}_")

    if rejected:
        lines.append("")
        lines.append("*⛔ 안전장치가 막음*")
        for e in rejected:
            side_ko = decision_ko(e["side"])
            conf_str = confidence_pct(e["confidence"])
            lines.append(
                f"- {e['name']} ({e['code']}) {side_ko} 확신도 {conf_str} — {e['reason']}"
            )

    telegram.send(
        settings.telegram,
        "\n".join(lines),
        reply_markup=cycle_summary_keyboard(),
    )
