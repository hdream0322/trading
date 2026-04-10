from __future__ import annotations

import json
import logging
import traceback as tb_module
from datetime import datetime
from typing import Any

from trading_bot.bot.commands import cycle_summary_keyboard
from trading_bot.config import Settings
from trading_bot.kis.client import KisClient
from trading_bot.notify import telegram
from trading_bot.risk import kill_switch
from trading_bot.risk.manager import RiskDecision, RiskManager
from trading_bot.signals import indicators, prefilter
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
            f"🚨 잔고 조회 실패로 사이클 중단\n`{exc}`",
        )
        return summary

    log.info(
        "잔고: 총평가 %s원 / 예수금 %s원 / 보유 %d종목 / 전일대비 %s%%",
        balance_summary.get("tot_evlu_amt", "?"),
        balance_summary.get("dnca_tot_amt", "?"),
        len(holdings),
        balance_summary.get("asst_icdc_erng_rt", "?"),
    )

    threshold = float(settings.llm.get("confidence_threshold", 0.75))
    executed_events: list[dict[str, Any]] = []

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
                    llm_reasoning="prefilter: 후보 아님",
                    llm_input_tokens=None, llm_output_tokens=None,
                    llm_cost_usd=None,
                )
                summary["hold"] += 1
                log.info("%s %s: hold (RSI=%.1f, vol=%.2fx)", code, name, rsi_val, vol_ratio)
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
                    llm_reasoning="LLM 비활성 (룰베이스 결정만 기록)",
                    llm_input_tokens=None, llm_output_tokens=None,
                    llm_cost_usd=None,
                )
                summary[candidate.side_hint] += 1
                log.info("%s %s → %s (LLM 비활성 — 주문 실행 안 함)", code, name, candidate.side_hint)
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


def _notify_summary(
    settings: Settings,
    summary: dict[str, Any],
    daily_cost: float,
    threshold: float,
    balance_summary: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    mode = settings.kis.mode
    mode_badge = "🔴 LIVE" if mode == "live" else "🟡 PAPER"
    lines = [
        f"*사이클 요약* {mode_badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총평가 {balance_summary.get('tot_evlu_amt', '?')}원 · 예수금 {balance_summary.get('dnca_tot_amt', '?')}원 · 전일대비 {balance_summary.get('asst_icdc_erng_rt', '?')}%",
        f"종목 {summary['total']} / 후보 {summary['candidates']} / 에러 {summary['errors']}",
        f"결정: buy {summary['buy']} · sell {summary['sell']} · hold {summary['hold']}",
        f"주문: 제출 {summary['orders_submitted']} · 리스크차단 {summary['orders_rejected_by_risk']}",
        f"LLM 비용(사이클) ${summary['cost_usd']:.4f} · 오늘 누적 ${daily_cost:.4f}",
    ]

    submitted = [e for e in events if e["type"] == "submitted"]
    rejected = [e for e in events if e["type"] == "rejected"]

    if submitted:
        lines.append("")
        lines.append("*✅ 주문 제출*")
        for e in submitted:
            side_emoji = "🟢" if e["side"] == "buy" else "🔴"
            lines.append(
                f"{side_emoji} {e['name']} ({e['code']}) {e['side']} "
                f"{e['qty']}주 @ ~{e['price']}원 conf {e['confidence']:.2f}"
            )
            lines.append(f"  주문번호 `{e['order_no']}`")
            lines.append(f"  _{e['reasoning'][:140]}_")

    if rejected:
        lines.append("")
        lines.append("*⛔ 리스크 차단*")
        for e in rejected:
            lines.append(
                f"- {e['name']} ({e['code']}) {e['side']} conf {e['confidence']:.2f} — {e['reason']}"
            )

    telegram.send(
        settings.telegram,
        "\n".join(lines),
        reply_markup=cycle_summary_keyboard(),
    )
