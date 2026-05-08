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

CREATE TABLE IF NOT EXISTS position_state (
    code TEXT PRIMARY KEY,
    name TEXT,
    entry_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    high_water_mark REAL NOT NULL,
    trailing_active INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fundamentals_cache (
    code TEXT PRIMARY KEY,
    name TEXT,
    per REAL,
    pbr REAL,
    roe REAL,
    eps REAL,
    bps REAL,
    debt_ratio REAL,
    dividend_yield REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    total_stocks INTEGER NOT NULL,
    candidates INTEGER NOT NULL DEFAULT 0,
    buy INTEGER NOT NULL DEFAULT 0,
    sell INTEGER NOT NULL DEFAULT 0,
    hold INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions_snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_cycle_runs_ts ON cycle_runs(ts);
"""


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # WAL: 동시 read/write 분리 — poller(텔레그램) 와 scheduler(사이클) 가
    # 다른 스레드에서 동시 write 시 'database is locked' 빈발하던 문제 완화.
    # busy_timeout: 잠시 대기 후 재시도 (즉시 OperationalError 회피).
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError as exc:
        log.warning("PRAGMA 적용 실패 (계속 진행): %s", exc)
    conn.executescript(SCHEMA)

    # 기존 DB에 컬럼 없으면 추가 (stage 1 DB에서 stage 2로 올라올 때)
    cur = conn.execute("PRAGMA table_info(signals)")
    cols = {row[1] for row in cur.fetchall()}
    if "llm_cost_usd" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN llm_cost_usd REAL")
        log.info("signals 테이블에 llm_cost_usd 컬럼 추가 (마이그레이션)")
    # v0.6.0 — 사후 정확도 트래킹 컬럼
    if "realized_return_pct" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN realized_return_pct REAL")
        log.info("signals 테이블에 realized_return_pct 컬럼 추가 (마이그레이션)")
    if "evaluated_at" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN evaluated_at TEXT")
        log.info("signals 테이블에 evaluated_at 컬럼 추가 (마이그레이션)")

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
