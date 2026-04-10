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
