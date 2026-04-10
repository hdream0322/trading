from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "trading.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    decision TEXT NOT NULL,            -- buy | sell | hold
    confidence REAL,
    rule_features TEXT,                -- JSON
    llm_model TEXT,
    llm_reasoning TEXT,
    llm_input_tokens INTEGER,
    llm_output_tokens INTEGER,
    llm_cost_usd REAL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,                -- buy | sell
    qty INTEGER NOT NULL,
    price INTEGER,                     -- 0 = 시장가
    mode TEXT NOT NULL,                -- dry-run | paper | live
    kis_order_no TEXT,
    status TEXT NOT NULL,              -- submitted | filled | partial | rejected | cancelled
    raw_response TEXT
);

CREATE TABLE IF NOT EXISTS positions_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    qty INTEGER NOT NULL,
    avg_price REAL,
    cur_price REAL,
    eval_amount REAL,
    pnl REAL,
    pnl_pct REAL
);

CREATE TABLE IF NOT EXISTS pnl_daily (
    date TEXT PRIMARY KEY,
    starting_equity REAL,
    ending_equity REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    trade_count INTEGER
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    traceback TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions_snapshot(ts);
"""


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    # 기존 DB에 컬럼 없으면 추가 (stage 1 DB에서 stage 2로 올라올 때)
    cur = conn.execute("PRAGMA table_info(signals)")
    cols = {row[1] for row in cur.fetchall()}
    if "llm_cost_usd" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN llm_cost_usd REAL")
        log.info("signals 테이블에 llm_cost_usd 컬럼 추가 (마이그레이션)")

    cur = conn.execute("PRAGMA table_info(orders)")
    cols_orders = {row[1] for row in cur.fetchall()}
    if "name" not in cols_orders:
        conn.execute("ALTER TABLE orders ADD COLUMN name TEXT")
        log.info("orders 테이블에 name 컬럼 추가 (마이그레이션)")
    if "reason" not in cols_orders:
        conn.execute("ALTER TABLE orders ADD COLUMN reason TEXT")
        log.info("orders 테이블에 reason 컬럼 추가 (마이그레이션)")

    conn.commit()
    log.info("SQLite 초기화 완료: %s", DB_PATH)
    return conn
