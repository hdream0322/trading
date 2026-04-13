"""/export — SQLite 조회 결과를 CSV 파일로 텔레그램 전송.

- 인자 없음: 버튼 메뉴
- /export signals       — 오늘 모든 사이클 결과 (RSI/거래량비/SMA/판정)
- /export nearmiss      — 오늘 1차 미달 종목 중 임계값에 가장 가까웠던 TOP20
- /export orders [N]    — 최근 N일 주문 (기본 7)
- /export errors [N]    — 최근 N일 에러 (기본 3)
- /export db            — data/trading.sqlite 통째로 (50MB 미만일 때만)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply, export_menu_keyboard
from trading_bot.store.db import DB_PATH

log = logging.getLogger(__name__)

# Telegram 업로드 한도 50MB. 여유 두고 49MB.
MAX_DOC_BYTES = 49 * 1024 * 1024


def _csv_bytes(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    # Excel 한글 깨짐 방지용 BOM
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")


def _doc_reply(filename: str, content: bytes, caption: str) -> dict[str, Any]:
    return {"document": (filename, content), "text": caption}


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ts_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def cmd_export(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not args:
        return _reply(
            "*📤 내보내기*\n"
            "원하는 항목을 골라주세요. 결과는 CSV 파일로 전송됩니다.",
            reply_markup=export_menu_keyboard(),
        )
    sub = args[0].strip().lower()
    rest = args[1:]
    if sub == "signals":
        return _export_signals()
    if sub == "nearmiss":
        return _export_nearmiss()
    if sub == "orders":
        days = _parse_days(rest, default=7)
        return _export_orders(days)
    if sub == "errors":
        days = _parse_days(rest, default=3)
        return _export_errors(days)
    if sub == "db":
        return _export_db()
    return _reply(
        f"모르는 항목: `{sub}`\n"
        "사용법: `/export` (메뉴) 또는 `signals` / `nearmiss` / `orders` / `errors` / `db`"
    )


def _parse_days(rest: list[str], default: int) -> int:
    if not rest:
        return default
    try:
        n = int(rest[0])
        return max(1, min(n, 90))
    except ValueError:
        return default


# ─────────────────────────────────────────────────────────────
# signals — 오늘 모든 사이클의 점검 결과
# ─────────────────────────────────────────────────────────────

def _export_signals() -> dict[str, Any]:
    today = _today_str()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """SELECT ts, code, name, decision, confidence,
                      rule_features, llm_model, llm_reasoning,
                      llm_input_tokens, llm_output_tokens, llm_cost_usd
               FROM signals
               WHERE substr(ts, 1, 10) = ?
               ORDER BY id ASC""",
            (today,),
        )
        raw = cur.fetchall()
        conn.close()
    except Exception as exc:
        return _reply(f"❌ 조회 실패\n`{exc}`")

    if not raw:
        return _reply("_오늘 기록이 아직 없습니다._")

    header = [
        "ts", "code", "name", "decision", "confidence",
        "rsi", "volume_ratio", "current_price", "sma_trend", "change_pct",
        "llm_model", "llm_reasoning",
        "input_tokens", "output_tokens", "cost_usd",
    ]
    rows = []
    for r in raw:
        feat = _safe_json(r[5])
        rows.append([
            r[0], r[1], r[2] or "", r[3], r[4],
            feat.get("rsi"), feat.get("volume_ratio"),
            feat.get("current_price"), feat.get("sma_trend"),
            feat.get("change_pct"),
            r[6] or "", (r[7] or "").replace("\n", " ")[:300],
            r[8], r[9], r[10],
        ])

    content = _csv_bytes(header, rows)
    caption = f"📋 *오늘 점검 전체* — {len(rows)}건 ({today})"
    return _doc_reply(f"signals_{_ts_tag()}.csv", content, caption)


# ─────────────────────────────────────────────────────────────
# nearmiss — 오늘 hold 중 임계값에 가장 가까웠던 종목 TOP20
# ─────────────────────────────────────────────────────────────

def _export_nearmiss() -> dict[str, Any]:
    today = _today_str()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """SELECT ts, code, name, rule_features, llm_reasoning
               FROM signals
               WHERE substr(ts, 1, 10) = ? AND decision = 'hold'
               ORDER BY id ASC""",
            (today,),
        )
        raw = cur.fetchall()
        conn.close()
    except Exception as exc:
        return _reply(f"❌ 조회 실패\n`{exc}`")

    if not raw:
        return _reply("_오늘 미달 기록이 없습니다._")

    rsi_buy = 35.0
    rsi_sell = 70.0
    min_vol = 1.2

    # 종목별로 가장 최근 데이터만 유지 (같은 종목 22번 사이클 → 가장 최근 1건)
    latest: dict[str, tuple] = {}
    for r in raw:
        latest[r[1]] = r  # code → row
    candidates = list(latest.values())

    scored: list[tuple[float, str, dict, str]] = []
    for r in candidates:
        feat = _safe_json(r[3])
        rsi = _to_float(feat.get("rsi"))
        vol = _to_float(feat.get("volume_ratio"))
        if rsi is None or vol is None:
            continue
        # buy 조건과의 거리: RSI가 35 이하여야 함 → max(0, rsi-35), 거래량 1.2배↑ → max(0, 1.2-vol)
        # sell 조건과의 거리: RSI가 70 이상이어야 함 → max(0, 70-rsi), 거래량 동일
        buy_dist = max(0.0, rsi - rsi_buy) + max(0.0, min_vol - vol) * 10
        sell_dist = max(0.0, rsi_sell - rsi) + max(0.0, min_vol - vol) * 10
        miss = min(buy_dist, sell_dist)
        scored.append((miss, r[1], feat, r[2] or ""))

    scored.sort(key=lambda x: x[0])
    top = scored[:20]

    if not top:
        return _reply("_평가 가능한 데이터가 없습니다._")

    header = [
        "rank", "miss_score", "code", "name",
        "rsi", "volume_ratio", "current_price", "sma_trend", "change_pct",
        "buy_distance", "sell_distance", "trend_ok",
    ]
    rows = []
    for i, (miss, code, feat, name) in enumerate(top, start=1):
        rsi = _to_float(feat.get("rsi"))
        vol = _to_float(feat.get("volume_ratio"))
        cur_p = _to_float(feat.get("current_price"))
        sma = _to_float(feat.get("sma_trend"))
        buy_dist = max(0.0, rsi - rsi_buy) + max(0.0, min_vol - vol) * 10
        sell_dist = max(0.0, rsi_sell - rsi) + max(0.0, min_vol - vol) * 10
        trend_ok = (cur_p is not None and sma is not None and cur_p > sma)
        rows.append([
            i, round(miss, 3), code, name,
            round(rsi, 2), round(vol, 3),
            cur_p, sma, feat.get("change_pct"),
            round(buy_dist, 3), round(sell_dist, 3),
            "Y" if trend_ok else "N",
        ])

    content = _csv_bytes(header, rows)
    caption = (
        f"🔍 *1차 미달 근접 TOP{len(rows)}* ({today})\n"
        f"_miss_score=0 에 가까울수록 통과 직전. "
        f"기준: RSI<{int(rsi_buy)} 또는 >{int(rsi_sell)}, 거래량 ≥{min_vol}배._"
    )
    return _doc_reply(f"nearmiss_{_ts_tag()}.csv", content, caption)


# ─────────────────────────────────────────────────────────────
# orders / errors
# ─────────────────────────────────────────────────────────────

def _export_orders(days: int) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """SELECT ts, code, name, side, qty, price, mode,
                      kis_order_no, status, reason
               FROM orders
               WHERE date(ts) >= date('now', ?, 'localtime')
               ORDER BY id DESC""",
            (f"-{days} days",),
        )
        raw = cur.fetchall()
        conn.close()
    except Exception as exc:
        return _reply(f"❌ 조회 실패\n`{exc}`")

    if not raw:
        return _reply(f"_최근 {days}일간 주문이 없습니다._")

    header = ["ts", "code", "name", "side", "qty", "price", "mode",
              "kis_order_no", "status", "reason"]
    rows = [list(r) for r in raw]
    content = _csv_bytes(header, rows)
    caption = f"🧾 *주문 내역* — 최근 {days}일 / {len(rows)}건"
    return _doc_reply(f"orders_{days}d_{_ts_tag()}.csv", content, caption)


def _export_errors(days: int) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """SELECT ts, component, message, traceback
               FROM errors
               WHERE date(ts) >= date('now', ?, 'localtime')
               ORDER BY id DESC""",
            (f"-{days} days",),
        )
        raw = cur.fetchall()
        conn.close()
    except Exception as exc:
        return _reply(f"❌ 조회 실패\n`{exc}`")

    if not raw:
        return _reply(f"_최근 {days}일간 에러가 없습니다. 👍_")

    header = ["ts", "component", "message", "traceback"]
    rows = []
    for r in raw:
        # traceback 은 길어서 1500자로 자름 (한 셀 안에서 줄바꿈 포함 OK)
        rows.append([r[0], r[1], r[2], (r[3] or "")[:1500]])
    content = _csv_bytes(header, rows)
    caption = f"🐞 *에러 로그* — 최근 {days}일 / {len(rows)}건"
    return _doc_reply(f"errors_{days}d_{_ts_tag()}.csv", content, caption)


# ─────────────────────────────────────────────────────────────
# db — SQLite 파일 통째로
# ─────────────────────────────────────────────────────────────

def _export_db() -> dict[str, Any]:
    if not DB_PATH.exists():
        return _reply("❌ DB 파일이 아직 없습니다.")
    size = DB_PATH.stat().st_size
    if size > MAX_DOC_BYTES:
        mb = size / 1024 / 1024
        return _reply(
            f"❌ DB 가 너무 큽니다 ({mb:.1f}MB). Telegram 한도 50MB 초과.\n"
            f"`/export signals` 등 분할 조회를 사용해주세요."
        )
    try:
        content = DB_PATH.read_bytes()
    except Exception as exc:
        return _reply(f"❌ DB 읽기 실패\n`{exc}`")
    mb = size / 1024 / 1024
    caption = f"🗄️ *trading.sqlite* — {mb:.2f}MB"
    return _doc_reply(f"trading_{_ts_tag()}.sqlite", content, caption)


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────

def _safe_json(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
