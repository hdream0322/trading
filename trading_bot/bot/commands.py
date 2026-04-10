from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Callable

from trading_bot.bot.context import BotContext
from trading_bot.kis.client import KisClient
from trading_bot.risk import kill_switch
from trading_bot.store import repo
from trading_bot.store.db import DB_PATH

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 응답 빌더 헬퍼
# ─────────────────────────────────────────────────────────────

def _reply(text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"text": text}
    if reply_markup is not None:
        out["reply_markup"] = reply_markup
    return out


def cycle_summary_keyboard() -> dict[str, Any]:
    """사이클 요약 메시지 하단에 붙는 퀵 액션 버튼."""
    return {
        "inline_keyboard": [
            [
                {"text": "🛑 긴급 정지", "callback_data": "kill"},
                {"text": "✅ 해제", "callback_data": "resume"},
            ],
            [
                {"text": "📊 포지션", "callback_data": "positions"},
                {"text": "💰 상태", "callback_data": "status"},
            ],
        ]
    }


def _sell_confirm_keyboard(code: str, name: str, qty: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": f"✅ {name} {qty}주 매도 확정", "callback_data": f"sell_confirm:{code}"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


# ─────────────────────────────────────────────────────────────
# 커맨드 핸들러
# ─────────────────────────────────────────────────────────────

HELP_TEXT = """*KIS 자동매매 봇 커맨드*

/status — 모드, 총자산, 전일대비, 킬스위치, LLM 비용
/positions — 현재 보유 종목 상세
/signals — 오늘 발생한 최근 시그널 10건
/cost — 오늘 LLM 누적 비용
/mode — 현재 거래 모드
/universe — 추적 중인 종목 목록

/stop — 🛑 킬스위치 활성 (신규 매수 전체 차단)
/resume — ✅ 킬스위치 해제
/sell 005930 — 특정 종목 전량 매도 (확정 버튼 필요)
/cycle — 지금 즉시 사이클 1회 강제 실행

/help — 이 도움말
"""


def cmd_help(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    return _reply(HELP_TEXT)


def cmd_mode(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    s = ctx.settings
    badge = "🔴 LIVE (실전)" if s.kis.mode == "live" else "🟡 PAPER (모의)"
    quote_mode = s.kis_quote.mode
    return _reply(
        f"*거래 모드*: {badge}\n"
        f"*시세 서버*: `{quote_mode}`\n"
        f"*계좌*: `{s.kis.account_no}-{s.kis.account_product_cd}`"
    )


def cmd_universe(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    lines = ["*유니버스*"]
    for item in ctx.settings.universe:
        lines.append(f"- {item['name']} (`{item['code']}`)")
    lines.append(f"\n사이클 주기: {ctx.settings.cycle_minutes}분")
    return _reply("\n".join(lines))


def cmd_status(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 잔고 조회 실패\n`{exc}`")
    bs = bal.get("summary", {}) or {}
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))

    kill_active = kill_switch.is_active()
    kill_badge = "🛑 활성" if kill_active else "✅ 해제"

    try:
        daily_cost = repo.today_llm_cost_usd()
    except Exception:
        daily_cost = 0.0
    try:
        today_orders = repo.get_today_order_count()
    except Exception:
        today_orders = 0

    badge = "🔴 LIVE" if ctx.settings.kis.mode == "live" else "🟡 PAPER"
    lines = [
        f"*상태* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총평가: `{bs.get('tot_evlu_amt', '?')}원`",
        f"예수금: `{bs.get('dnca_tot_amt', '?')}원`",
        f"전일 대비: `{bs.get('asst_icdc_erng_rt', '?')}%`",
        f"보유 종목: {len(holdings)}개",
        f"킬스위치: {kill_badge}",
        f"오늘 주문: {today_orders}건 / LLM 비용 ${daily_cost:.4f}",
    ]
    return _reply("\n".join(lines), reply_markup=cycle_summary_keyboard())


def cmd_positions(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 잔고 조회 실패\n`{exc}`")
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))
    if not holdings:
        return _reply("보유 종목 없음")
    lines = ["*보유 포지션*"]
    for code, p in holdings.items():
        pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
        lines.append(
            f"{pnl_emoji} *{p['name']}* (`{code}`)\n"
            f"   {p['qty']}주 · 평단 {p['avg_price']:.0f} · 현재 {p['cur_price']:.0f}\n"
            f"   평가 {p['eval_amount']:.0f}원 · 손익 {p['pnl']:+.0f}원 ({p['pnl_pct']:+.2f}%)"
        )
    return _reply("\n".join(lines))


def cmd_signals(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """SELECT substr(ts, 12, 5), code, name, decision, confidence, substr(llm_reasoning, 1, 80)
               FROM signals
               WHERE substr(ts, 1, 10) = ?
               ORDER BY id DESC LIMIT 10""",
            (datetime.now().strftime("%Y-%m-%d"),),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as exc:
        return _reply(f"❌ DB 조회 실패\n`{exc}`")
    if not rows:
        return _reply("오늘 발생한 시그널 없음")
    lines = ["*오늘 시그널 (최근 10)*"]
    for t, code, name, decision, conf, reason in rows:
        conf_str = f" {conf:.2f}" if conf is not None else ""
        emoji = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}.get(decision, "❓")
        lines.append(f"{emoji} `{t}` {name or code} → *{decision}*{conf_str}")
        if reason:
            lines.append(f"   _{reason}_")
    return _reply("\n".join(lines))


def cmd_cost(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    try:
        daily_cost = repo.today_llm_cost_usd()
    except Exception as exc:
        return _reply(f"❌ 비용 조회 실패\n`{exc}`")
    limit = float(ctx.settings.llm.get("daily_cost_limit_usd", 5.0))
    pct = (daily_cost / limit * 100) if limit > 0 else 0
    return _reply(
        f"*오늘 LLM 비용*\n"
        f"사용: `${daily_cost:.4f}` / 한도 `${limit:.2f}` ({pct:.1f}%)"
    )


def cmd_stop(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if kill_switch.is_active():
        return _reply("🛑 이미 킬스위치가 활성 상태입니다")
    kill_switch.activate(reason="telegram /stop command")
    return _reply("🛑 *킬스위치 활성화*\n신규 매수 전체 차단. 매도(손절/청산)는 계속 허용.\n`/resume` 으로 해제.")


def cmd_resume(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not kill_switch.is_active():
        return _reply("✅ 킬스위치는 이미 해제 상태입니다")
    kill_switch.deactivate()
    return _reply("✅ *킬스위치 해제*\n다음 사이클부터 신규 매수 가능.")


def cmd_sell(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not args:
        return _reply("사용법: `/sell 종목코드` (예: `/sell 005930`)")
    code = args[0].strip()
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 잔고 조회 실패\n`{exc}`")
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))
    if code not in holdings:
        return _reply(f"`{code}` 은(는) 미보유 종목입니다")
    p = holdings[code]
    text = (
        f"*매도 확정 요청*\n"
        f"{p['name']} (`{code}`)\n"
        f"수량: *{p['qty']}주*\n"
        f"평단 {p['avg_price']:.0f}원 · 현재 {p['cur_price']:.0f}원\n"
        f"평가손익 {p['pnl']:+.0f}원 ({p['pnl_pct']:+.2f}%)"
    )
    return _reply(text, reply_markup=_sell_confirm_keyboard(code, p["name"], p["qty"]))


def cmd_cycle(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    # 사이클 즉시 실행. poller 스레드에서 동기 실행 (20~30초 소요).
    # run_cycle은 내부적으로 trading_lock 없이 돌지만, 수동 /sell과 경합 방지를 위해 여기서 락.
    from trading_bot.signals.cycle import run_cycle

    with ctx.trading_lock:
        try:
            summary = run_cycle(ctx.settings, ctx.kis, ctx.llm, ctx.risk)
        except Exception as exc:
            log.exception("수동 사이클 실행 실패")
            return _reply(f"❌ 사이클 실행 중 예외\n`{exc}`")
    return _reply(
        f"*사이클 완료*\n"
        f"후보 {summary.get('candidates', 0)} · "
        f"buy {summary.get('buy', 0)} · sell {summary.get('sell', 0)} · hold {summary.get('hold', 0)}\n"
        f"주문 제출 {summary.get('orders_submitted', 0)} · 리스크차단 {summary.get('orders_rejected_by_risk', 0)}\n"
        f"LLM 비용 ${summary.get('cost_usd', 0):.4f}"
    )


COMMAND_MAP: dict[str, Callable[[BotContext, list[str]], dict[str, Any]]] = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/mode": cmd_mode,
    "/universe": cmd_universe,
    "/status": cmd_status,
    "/positions": cmd_positions,
    "/signals": cmd_signals,
    "/cost": cmd_cost,
    "/stop": cmd_stop,
    "/kill": cmd_stop,
    "/resume": cmd_resume,
    "/sell": cmd_sell,
    "/cycle": cmd_cycle,
}


def handle_command(ctx: BotContext, cmd: str, args: list[str]) -> dict[str, Any] | None:
    handler = COMMAND_MAP.get(cmd.lower())
    if handler is None:
        return _reply(f"알 수 없는 커맨드: `{cmd}`\n`/help` 로 목록 확인")
    try:
        return handler(ctx, args)
    except Exception as exc:
        log.exception("커맨드 %s 처리 중 예외", cmd)
        return _reply(f"❌ 커맨드 처리 실패\n`{type(exc).__name__}: {exc}`")


# ─────────────────────────────────────────────────────────────
# 콜백 (inline 버튼) 핸들러
# ─────────────────────────────────────────────────────────────

def handle_callback(ctx: BotContext, data: str) -> dict[str, Any] | None:
    if data == "cancel":
        return _reply("취소됨")
    if data == "kill":
        return cmd_stop(ctx, [])
    if data == "resume":
        return cmd_resume(ctx, [])
    if data == "positions":
        return cmd_positions(ctx, [])
    if data == "status":
        return cmd_status(ctx, [])
    if data.startswith("sell_confirm:"):
        code = data.split(":", 1)[1]
        return _execute_confirmed_sell(ctx, code)
    return _reply(f"알 수 없는 콜백: `{data}`")


def _execute_confirmed_sell(ctx: BotContext, code: str) -> dict[str, Any]:
    """매도 확정 버튼 처리. trading_lock으로 사이클과 직렬화."""
    with ctx.trading_lock:
        try:
            bal = ctx.kis.get_balance()
        except Exception as exc:
            return _reply(f"❌ 잔고 조회 실패\n`{exc}`")
        holdings = KisClient.normalize_holdings(bal.get("holdings", []))
        if code not in holdings:
            return _reply(f"`{code}` 은(는) 이미 미보유 상태입니다")
        p = holdings[code]
        qty = int(p["qty"])
        try:
            result = ctx.kis.place_market_order(code, "sell", qty)
        except Exception as exc:
            repo.insert_error(component="manual_sell", message=f"{code} {qty}: {exc}")
            return _reply(f"❌ *매도 실패*\n{p['name']} ({code})\n`{exc}`")

        order_no = result["order_no"]
        repo.insert_order(
            ts=datetime.now().isoformat(timespec="seconds"),
            code=code,
            name=p["name"],
            side="sell",
            qty=qty,
            price=None,
            mode=ctx.settings.kis.mode,
            kis_order_no=order_no,
            status="submitted",
            raw_response=json.dumps(result["raw"], ensure_ascii=False)[:2000],
            reason="manual sell via telegram",
        )
    return _reply(
        f"✅ *매도 제출*\n"
        f"{p['name']} ({code})\n"
        f"{qty}주 시장가\n"
        f"주문번호 `{order_no}`"
    )
