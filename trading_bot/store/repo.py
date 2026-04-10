from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

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
