from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

from trading_bot.store.db import DB_PATH

log = logging.getLogger(__name__)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def insert_signal(
    ts: str,
    code: str,
    name: str | None,
    decision: str,
    confidence: float | None,
    rule_features: str | None,
    llm_model: str | None,
    llm_reasoning: str | None,
    llm_input_tokens: int | None,
    llm_output_tokens: int | None,
    llm_cost_usd: float | None,
) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO signals
               (ts, code, name, decision, confidence, rule_features,
                llm_model, llm_reasoning, llm_input_tokens, llm_output_tokens, llm_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts, code, name, decision, confidence, rule_features,
                llm_model, llm_reasoning, llm_input_tokens, llm_output_tokens, llm_cost_usd,
            ),
        )


def today_llm_cost_usd() -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        cur = conn.execute(
            "SELECT COALESCE(SUM(llm_cost_usd), 0) FROM signals WHERE substr(ts, 1, 10) = ?",
            (today,),
        )
        return float(cur.fetchone()[0])


def insert_error(component: str, message: str, traceback: str | None = None) -> None:
    ts = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO errors (ts, component, message, traceback) VALUES (?, ?, ?, ?)",
            (ts, component, message, traceback),
        )


def get_pending_orders_today() -> list[dict[str, Any]]:
    """오늘 status='submitted' 이면서 kis_order_no 가 있는 주문들.

    체결 추적 잡이 확인해야 할 대상.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        cur = conn.execute(
            """SELECT id, code, name, side, qty, kis_order_no
                 FROM orders
                WHERE substr(ts, 1, 10) = ?
                  AND status = 'submitted'
                  AND kis_order_no IS NOT NULL
                  AND kis_order_no != ''
                ORDER BY id ASC""",
            (today,),
        )
        return [
            {
                "id": int(r[0]),
                "code": r[1],
                "name": r[2],
                "side": r[3],
                "qty": int(r[4] or 0),
                "kis_order_no": r[5],
            }
            for r in cur.fetchall()
        ]


def update_order_status(
    order_id: int,
    status: str,
    reason: str | None = None,
    price: int | None = None,
) -> None:
    """주문 상태 업데이트 — filled / partial / cancelled / failed 등."""
    with _conn() as conn:
        if price is not None:
            conn.execute(
                "UPDATE orders SET status = ?, reason = COALESCE(?, reason), price = ? WHERE id = ?",
                (status, reason, price, order_id),
            )
        else:
            conn.execute(
                "UPDATE orders SET status = ?, reason = COALESCE(?, reason) WHERE id = ?",
                (status, reason, order_id),
            )


def count_recent_errors(minutes: int = 60) -> int:
    """최근 N분간 errors 테이블에 쌓인 에러 건수.

    회로차단기(에러 급증 감지) 용도.
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM errors WHERE ts >= ?",
            (cutoff,),
        )
        return int(cur.fetchone()[0])


def insert_order(
    ts: str,
    code: str,
    name: str | None,
    side: str,
    qty: int,
    price: int | None,
    mode: str,
    kis_order_no: str | None,
    status: str,
    raw_response: str | None,
    reason: str | None = None,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO orders
               (ts, code, name, side, qty, price, mode, kis_order_no, status, raw_response, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, code, name, side, qty, price, mode, kis_order_no, status, raw_response, reason),
        )
        return int(cur.lastrowid or 0)


def get_last_order_ts(code: str) -> str | None:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT ts FROM orders WHERE code = ? ORDER BY id DESC LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_today_order_count() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE substr(ts, 1, 10) = ? AND status != 'rejected'",
            (today,),
        )
        return int(cur.fetchone()[0])


def get_today_orders() -> list[dict[str, object]]:
    """오늘 들어온 주문 내역 (rejected 포함). 장 마감 브리핑 요약용."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        cur = conn.execute(
            """SELECT ts, code, name, side, qty, status, reason
                 FROM orders
                WHERE substr(ts, 1, 10) = ?
                ORDER BY id ASC""",
            (today,),
        )
        return [
            {
                "ts": r[0],
                "code": r[1],
                "name": r[2],
                "side": r[3],
                "qty": int(r[4] or 0),
                "status": r[5],
                "reason": r[6],
            }
            for r in cur.fetchall()
        ]


def record_cycle_run(
    ts: str,
    total_stocks: int,
    candidates: int,
    buy: int,
    sell: int,
    hold: int,
    errors: int,
    cost_usd: float,
) -> None:
    """사이클 실행 1회를 cycle_runs 테이블에 기록."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO cycle_runs
               (ts, total_stocks, candidates, buy, sell, hold, errors, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, total_stocks, candidates, buy, sell, hold, errors, cost_usd),
        )


def get_today_signal_summary() -> dict[str, int]:
    """오늘 사이클에서 나온 판단(decision) 카운트 + 차단 사유 집계.

    장 마감 브리핑 "왜 거래가 없었나" 사후 설명 및 /signals 통계용.
    total_checks: cycle_runs 테이블 기준 (사이클 실행 횟수).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    out: dict[str, int] = {
        "total_checks": 0,
        "prefilter_pass": 0,
        "llm_buy": 0,
        "llm_sell": 0,
        "llm_hold": 0,
        "low_confidence": 0,
        "risk_rejected": 0,
    }
    with _conn() as conn:
        # 사이클 실행 횟수는 cycle_runs 테이블 기준
        cur0 = conn.execute(
            "SELECT COUNT(*) FROM cycle_runs WHERE substr(ts, 1, 10) = ?",
            (today,),
        )
        out["total_checks"] = int(cur0.fetchone()[0])

        cur = conn.execute(
            """SELECT decision, confidence, llm_reasoning
                 FROM signals WHERE substr(ts, 1, 10) = ?""",
            (today,),
        )
        for decision, confidence, reasoning in cur.fetchall():
            rtext = reasoning or ""
            if "1차 조건 통과 못함" in rtext:
                continue
            out["prefilter_pass"] += 1
            if decision == "buy":
                out["llm_buy"] += 1
            elif decision == "sell":
                out["llm_sell"] += 1
            else:
                out["llm_hold"] += 1
            if confidence is not None and float(confidence) < 0.75 and decision != "hold":
                out["low_confidence"] += 1

        cur2 = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE substr(ts, 1, 10) = ? AND status = 'rejected'",
            (today,),
        )
        out["risk_rejected"] = int(cur2.fetchone()[0])
    return out


def get_today_risk_rejection_reasons() -> list[tuple[str, int]]:
    """오늘 리스크 매니저가 차단한 주문의 사유별 카운트.

    예: [("쿨다운", 3), ("동시 보유 종목 수", 2)]
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        cur = conn.execute(
            """SELECT reason FROM orders
                WHERE substr(ts, 1, 10) = ? AND status = 'rejected'""",
            (today,),
        )
        buckets: dict[str, int] = {}
        for (reason,) in cur.fetchall():
            key = _bucket_risk_reason(reason or "")
            buckets[key] = buckets.get(key, 0) + 1
    return sorted(buckets.items(), key=lambda x: -x[1])


def _bucket_risk_reason(reason: str) -> str:
    r = reason or ""
    if "쿨다운" in r or "cooldown" in r.lower():
        return "쿨다운"
    if "킬스위치" in r or "킬 스위치" in r or "kill" in r.lower():
        return "긴급 정지"
    if "주문 횟수 제한" in r:
        return "일일 주문 한도"
    if "일일 손실" in r:
        return "일일 손실 한도"
    if "중복" in r or "이미 보유" in r:
        return "중복 진입"
    if "동시 보유" in r or "max_concurrent" in r.lower():
        return "동시 보유 상한"
    if "포지션" in r and ("사이징" in r or "금액" in r or "%" in r):
        return "포지션 사이징"
    return "기타"


def upsert_pnl_daily(
    date: str,
    starting_equity: float | None,
    ending_equity: float,
    realized_pnl: float | None,
    unrealized_pnl: float | None,
    trade_count: int,
) -> None:
    """장 마감 브리핑에서 오늘 실적을 기록. date 중복 시 갱신 (INSERT OR REPLACE)."""
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pnl_daily
               (date, starting_equity, ending_equity, realized_pnl, unrealized_pnl, trade_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (date, starting_equity, ending_equity, realized_pnl, unrealized_pnl, trade_count),
        )


def monthly_llm_cost_usd() -> float:
    """이번 달 누적 LLM 비용."""
    month_prefix = datetime.now().strftime("%Y-%m")
    with _conn() as conn:
        cur = conn.execute(
            "SELECT COALESCE(SUM(llm_cost_usd), 0) FROM signals WHERE substr(ts, 1, 7) = ?",
            (month_prefix,),
        )
        return float(cur.fetchone()[0])


def get_recent_pnl_daily(days: int = 7) -> list[dict[str, Any]]:
    """최근 N일 pnl_daily 레코드. 주간/월간 통계 조회용."""
    with _conn() as conn:
        cur = conn.execute(
            """SELECT date, starting_equity, ending_equity, realized_pnl,
                      unrealized_pnl, trade_count
                 FROM pnl_daily
                ORDER BY date DESC
                LIMIT ?""",
            (days,),
        )
        return [
            {
                "date": r[0],
                "starting_equity": r[1],
                "ending_equity": r[2],
                "realized_pnl": r[3],
                "unrealized_pnl": r[4],
                "trade_count": int(r[5] or 0),
            }
            for r in cur.fetchall()
        ]


# ─────────────────────────────────────────────────────────────
# position_state — 손절/익절/트레일링 스톱 상태 추적
# ─────────────────────────────────────────────────────────────

def get_all_position_states() -> dict[str, dict[str, object]]:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT code, name, entry_ts, entry_price, high_water_mark, trailing_active FROM position_state"
        )
        result: dict[str, dict[str, object]] = {}
        for row in cur.fetchall():
            result[row[0]] = {
                "code": row[0],
                "name": row[1],
                "entry_ts": row[2],
                "entry_price": float(row[3]),
                "high_water_mark": float(row[4]),
                "trailing_active": bool(row[5]),
            }
        return result


def insert_position_state(
    code: str,
    name: str | None,
    entry_ts: str,
    entry_price: float,
    high_water_mark: float,
    trailing_active: bool = False,
) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO position_state
               (code, name, entry_ts, entry_price, high_water_mark, trailing_active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code, name, entry_ts, entry_price, high_water_mark, 1 if trailing_active else 0),
        )


def update_position_hwm(code: str, high_water_mark: float, trailing_active: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE position_state SET high_water_mark = ?, trailing_active = ? WHERE code = ?",
            (high_water_mark, 1 if trailing_active else 0, code),
        )


def delete_position_state(code: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM position_state WHERE code = ?", (code,))


# ─────────────────────────────────────────────────────────────
# 사후 정확도 트래킹 (v0.6.0)
# ─────────────────────────────────────────────────────────────

def get_signals_awaiting_eval(cutoff_ts: str) -> list[dict[str, Any]]:
    """평가 대상 signal — decision 이 buy/sell 이고 N거래일 경과했지만 아직
    realized_return_pct 가 NULL 인 것들.

    cutoff_ts: 이 시각보다 오래된 signal 만 대상 (ISO format).
    """
    with _conn() as conn:
        cur = conn.execute(
            """SELECT id, ts, code, name, decision, confidence
                 FROM signals
                WHERE decision IN ('buy', 'sell')
                  AND realized_return_pct IS NULL
                  AND ts <= ?
                ORDER BY ts ASC
                LIMIT 500""",
            (cutoff_ts,),
        )
        return [
            {
                "id": int(r[0]),
                "ts": r[1],
                "code": r[2],
                "name": r[3],
                "decision": r[4],
                "confidence": r[5],
            }
            for r in cur.fetchall()
        ]


def update_signal_forward_return(
    signal_id: int,
    realized_return_pct: float,
    evaluated_at: str,
) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE signals
                  SET realized_return_pct = ?,
                      evaluated_at = ?
                WHERE id = ?""",
            (realized_return_pct, evaluated_at, signal_id),
        )


def get_accuracy_by_confidence_bucket() -> list[dict[str, Any]]:
    """confidence 구간별 적중률/평균수익률/건수 집계.

    "적중" 정의:
      - buy: realized_return_pct >= +1%
      - sell: realized_return_pct <= -1%
    구간: [0.75, 0.80), [0.80, 0.85), [0.85, 0.90), [0.90, 1.01)
    """
    buckets = [(0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 1.01)]
    out: list[dict[str, Any]] = []
    with _conn() as conn:
        for low, high in buckets:
            cur = conn.execute(
                """SELECT decision, realized_return_pct
                     FROM signals
                    WHERE decision IN ('buy', 'sell')
                      AND confidence >= ? AND confidence < ?
                      AND realized_return_pct IS NOT NULL""",
                (low, high),
            )
            rows = cur.fetchall()
            if not rows:
                out.append({
                    "low": low, "high": high,
                    "count": 0, "hit_rate": 0.0, "avg_return": 0.0,
                })
                continue
            total = len(rows)
            hits = 0
            total_return = 0.0
            for decision, ret in rows:
                r = float(ret or 0)
                total_return += r
                if decision == "buy" and r >= 1.0:
                    hits += 1
                elif decision == "sell" and r <= -1.0:
                    hits += 1
            out.append({
                "low": low, "high": high,
                "count": total,
                "hit_rate": hits / total * 100.0,
                "avg_return": total_return / total,
            })
    return out


def get_accuracy_by_cross_check() -> dict[str, dict[str, Any]]:
    """교차검증 태그별 사후 수익률 집계.

    [DIRECTION_CONFLICT] / [LLM_HOLD] 태그가 붙은 signal 의 사후 수익률이
    prefilter 방향으로 봤을 때 맞았는지 / 틀렸는지 확인 — LLM vs prefilter
    중 누가 더 정확했는지 판단 근거.
    """
    result: dict[str, dict[str, Any]] = {
        "DIRECTION_CONFLICT": {"count": 0, "avg_return": 0.0, "sum_return": 0.0},
        "LLM_HOLD": {"count": 0, "avg_return": 0.0, "sum_return": 0.0},
    }
    with _conn() as conn:
        cur = conn.execute(
            """SELECT llm_reasoning, realized_return_pct
                 FROM signals
                WHERE realized_return_pct IS NOT NULL
                  AND llm_reasoning LIKE '[%'""",
        )
        for reasoning, ret in cur.fetchall():
            if not reasoning or ret is None:
                continue
            r = float(ret)
            tag: str | None = None
            if reasoning.startswith("[DIRECTION_CONFLICT"):
                tag = "DIRECTION_CONFLICT"
            elif reasoning.startswith("[LLM_HOLD"):
                tag = "LLM_HOLD"
            if tag:
                result[tag]["count"] += 1
                result[tag]["sum_return"] += r
    for tag in result:
        c = result[tag]["count"]
        if c > 0:
            result[tag]["avg_return"] = result[tag]["sum_return"] / c
    return result


# ─────────────────────────────────────────────────────────────
# fundamentals_cache — 재무지표 캐시 (Stage 10)
# ─────────────────────────────────────────────────────────────

def get_fundamentals_cache(code: str) -> dict[str, Any] | None:
    """fundamentals_cache 에서 단일 종목 조회. 없으면 None."""
    with _conn() as conn:
        cur = conn.execute(
            """SELECT code, name, per, pbr, roe, eps, bps,
                      debt_ratio, dividend_yield, updated_at
                 FROM fundamentals_cache WHERE code = ?""",
            (code,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "code": row[0],
            "name": row[1],
            "per": row[2],
            "pbr": row[3],
            "roe": row[4],
            "eps": row[5],
            "bps": row[6],
            "debt_ratio": row[7],
            "dividend_yield": row[8],
            "updated_at": row[9],
        }


def upsert_fundamentals_cache(
    code: str,
    name: str | None,
    per: float | None,
    pbr: float | None,
    roe: float | None,
    eps: float | None,
    bps: float | None,
    debt_ratio: float | None,
    dividend_yield: float | None,
    updated_at: str,
) -> None:
    """INSERT OR REPLACE 로 재무지표 캐시 갱신."""
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fundamentals_cache
               (code, name, per, pbr, roe, eps, bps, debt_ratio, dividend_yield, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, name, per, pbr, roe, eps, bps, debt_ratio, dividend_yield, updated_at),
        )


def get_all_fundamentals_cache() -> dict[str, dict[str, Any]]:
    """전체 캐시 조회. {code: {...}} 맵."""
    with _conn() as conn:
        cur = conn.execute(
            """SELECT code, name, per, pbr, roe, eps, bps,
                      debt_ratio, dividend_yield, updated_at
                 FROM fundamentals_cache"""
        )
        result: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            result[row[0]] = {
                "code": row[0],
                "name": row[1],
                "per": row[2],
                "pbr": row[3],
                "roe": row[4],
                "eps": row[5],
                "bps": row[6],
                "debt_ratio": row[7],
                "dividend_yield": row[8],
                "updated_at": row[9],
            }
        return result
