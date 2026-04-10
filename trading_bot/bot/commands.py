from __future__ import annotations

import json
import logging
import platform
import sqlite3
import sys
from datetime import datetime
from typing import Any, Callable

from trading_bot import __version__ as bot_version
from trading_bot.bot import update_manager
from trading_bot.bot.context import BotContext
from trading_bot.kis.client import KisClient
from trading_bot.risk import kill_switch
from trading_bot.store import repo
from trading_bot.store.db import DB_PATH

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Telegram / 자동완성 메뉴에 등록될 커맨드 목록
# 봇 기동 시 telegram.set_commands(..., TELEGRAM_BOT_COMMANDS) 호출
# ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_COMMANDS: list[tuple[str, str]] = [
    ("help", "사용법 보기"),
    ("status", "지금 상태 (자산·킬스위치·비용)"),
    ("positions", "갖고 있는 주식"),
    ("signals", "오늘 매매 추천 (최근 10개)"),
    ("cost", "오늘 AI 분석 비용"),
    ("mode", "지금 거래 모드 (실전/모의)"),
    ("universe", "추적 중인 종목 목록"),
    ("about", "봇 버전 및 전체 설정"),
    ("stop", "🛑 긴급 정지 (새로 구매 차단)"),
    ("resume", "✅ 긴급 정지 풀기"),
    ("sell", "특정 종목 전부 팔기 (확인 필요)"),
    ("cycle", "지금 바로 점검 실행"),
    ("update", "최신 버전 확인"),
]


# ─────────────────────────────────────────────────────────────
# 포맷 헬퍼 — 토스 증권처럼 읽기 쉽게
# ─────────────────────────────────────────────────────────────

def fmt_won(value: Any, fallback: str = "?") -> str:
    """10000000 → '10,000,000원'"""
    try:
        return f"{int(float(value)):,}원"
    except (ValueError, TypeError):
        return fallback


def fmt_pct(value: Any, fallback: str = "?") -> str:
    """부호 붙여서 퍼센트 표시. 1.23 → '+1.23%'"""
    try:
        return f"{float(value):+.2f}%"
    except (ValueError, TypeError):
        return fallback


def decision_ko(decision: str) -> str:
    """buy/sell/hold → 구매/판매/관망"""
    return {"buy": "구매", "sell": "판매", "hold": "관망"}.get(decision, decision)


def mode_badge(mode: str) -> str:
    return "🔴 실전" if mode == "live" else "🟡 모의"


def confidence_pct(value: float | None) -> str:
    """0.75 → '75%'"""
    if value is None:
        return ""
    return f"{int(round(value * 100))}%"


def fmt_uptime(delta_seconds: float) -> str:
    """초 단위 업타임을 '3일 2시간 15분' 형태로."""
    total = int(delta_seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    parts.append(f"{minutes}분")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────
# 응답 빌더
# ─────────────────────────────────────────────────────────────

def _reply(text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"text": text}
    if reply_markup is not None:
        out["reply_markup"] = reply_markup
    return out


def cycle_summary_keyboard() -> dict[str, Any]:
    """점검 결과 메시지 하단 퀵 액션 버튼."""
    return {
        "inline_keyboard": [
            [
                {"text": "🛑 긴급 정지", "callback_data": "kill"},
                {"text": "✅ 해제", "callback_data": "resume"},
            ],
            [
                {"text": "📊 내 주식", "callback_data": "positions"},
                {"text": "💰 상태", "callback_data": "status"},
            ],
        ]
    }


def _sell_confirm_keyboard(code: str, name: str, qty: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": f"✅ {name} {qty}주 판매 확정", "callback_data": f"sell_confirm:{code}"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


# ─────────────────────────────────────────────────────────────
# 커맨드 핸들러
# ─────────────────────────────────────────────────────────────

HELP_TEXT = """*자동매매 봇 사용법*

*📊 조회*
/status — 지금 상태 (모드, 총 자산, 긴급정지, AI 비용)
/positions — 갖고 있는 주식 상세
/signals — 오늘 나온 매매 추천 (최근 10개)
/cost — 오늘 AI 분석 비용
/mode — 지금 거래 모드 (실전/모의)
/universe — 추적 중인 종목 목록
/about — 봇 버전, 가동 시간, 전체 설정 요약

*⚙️ 조작*
/stop — 🛑 긴급 정지 (새로 구매 안 함)
/resume — ✅ 긴급 정지 풀기
/sell 005930 — 특정 종목 전부 팔기 (한 번 더 확인)
/cycle — 지금 바로 점검 한 번 돌리기

*🔄 업데이트*
/update — 최신 버전 확인 (현재/최신 버전 표시만)
/update confirm — 실제 업데이트 실행
/update enable — 자동 업데이트 켜기 (매일 02:00 KST)
/update disable — 자동 업데이트 끄기
/update status — 자동 업데이트 상태 확인

/help — 이 도움말

---
_매 10분마다 봇이 관심 종목들을 점검합니다._
_조건이 맞으면 AI가 판단해서 자동으로 주문을 넣습니다._
_모든 매매는 안전장치 7단계를 거쳐야 실행됩니다._
"""


def cmd_help(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    return _reply(HELP_TEXT)


def cmd_mode(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    s = ctx.settings
    badge = mode_badge(s.kis.mode)
    mode_desc = "실제 돈이 움직입니다" if s.kis.mode == "live" else "가상 거래 — 실제 돈 움직이지 않음"
    quote_mode = "실전 서버" if s.kis_quote.mode == "live" else "모의 서버"
    return _reply(
        f"*지금 모드*\n"
        f"거래: {badge}\n"
        f"   _{mode_desc}_\n"
        f"시세 조회: {quote_mode}\n"
        f"계좌: `{s.kis.account_no}-{s.kis.account_product_cd}`"
    )


def cmd_universe(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    lines = ["*추적 중인 종목*"]
    for item in ctx.settings.universe:
        lines.append(f"- {item['name']} (`{item['code']}`)")
    lines.append(f"\n점검 주기: {ctx.settings.cycle_minutes}분")
    return _reply("\n".join(lines))


def cmd_status(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 계좌 조회 실패\n`{exc}`")
    bs = bal.get("summary", {}) or {}
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))

    kill_active = kill_switch.is_active()
    kill_badge = "🛑 켜짐" if kill_active else "✅ 꺼짐"

    try:
        daily_cost = repo.today_llm_cost_usd()
    except Exception:
        daily_cost = 0.0
    try:
        today_orders = repo.get_today_order_count()
    except Exception:
        today_orders = 0

    badge = mode_badge(ctx.settings.kis.mode)
    lines = [
        f"*지금 상태* {badge} — {datetime.now():%Y-%m-%d %H:%M}",
        f"총 자산: `{fmt_won(bs.get('tot_evlu_amt'))}`",
        f"쓸 수 있는 현금: `{fmt_won(bs.get('dnca_tot_amt'))}`",
        f"어제 대비: `{fmt_pct(bs.get('asst_icdc_erng_rt'))}`",
        f"갖고 있는 주식: {len(holdings)}개",
        f"긴급 정지: {kill_badge}",
        f"오늘 주문: {today_orders}건 / AI 분석 비용 ${daily_cost:.4f}",
    ]
    return _reply("\n".join(lines), reply_markup=cycle_summary_keyboard())


def cmd_positions(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 계좌 조회 실패\n`{exc}`")
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))
    if not holdings:
        return _reply("갖고 있는 주식이 없습니다")
    lines = ["*갖고 있는 주식*"]
    for code, p in holdings.items():
        pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
        lines.append(
            f"{pnl_emoji} *{p['name']}* (`{code}`)\n"
            f"   {p['qty']}주 · 평균 구매가 {int(p['avg_price']):,}원 · 지금 가격 {int(p['cur_price']):,}원\n"
            f"   지금 평가 {int(p['eval_amount']):,}원 · 손익 {int(p['pnl']):+,}원 ({p['pnl_pct']:+.2f}%)"
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
        return _reply(f"❌ 기록 조회 실패\n`{exc}`")
    if not rows:
        return _reply("오늘 나온 추천이 아직 없습니다")
    lines = ["*오늘 매매 추천 (최근 10개)*"]
    for t, code, name, decision, conf, reason in rows:
        conf_str = f" {confidence_pct(conf)}" if conf is not None else ""
        emoji = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}.get(decision, "❓")
        lines.append(f"{emoji} `{t}` {name or code} → *{decision_ko(decision)}*{conf_str}")
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
        f"*오늘 AI 분석 비용*\n"
        f"사용: `${daily_cost:.4f}` / 한도 `${limit:.2f}` ({pct:.1f}%)\n\n"
        f"_AI (Claude)가 종목별 매매 판단을 내릴 때마다 청구됩니다._\n"
        f"_한도에 도달하면 남은 시간동안 AI 판단 없이 관망합니다._"
    )


def cmd_stop(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if kill_switch.is_active():
        return _reply("🛑 긴급 정지가 이미 켜져있습니다")
    kill_switch.activate(reason="telegram /stop command")
    return _reply(
        "🛑 *긴급 정지 켜짐*\n"
        "새로 구매는 막힙니다.\n"
        "이미 갖고 있는 주식은 필요 시 자동 판매가 계속됩니다 (손절/청산용).\n\n"
        "`/resume` 으로 다시 풀 수 있습니다."
    )


def cmd_resume(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not kill_switch.is_active():
        return _reply("✅ 긴급 정지는 이미 꺼져있습니다")
    kill_switch.deactivate()
    return _reply("✅ *긴급 정지 풀림*\n다음 점검부터 새로 구매가 가능합니다.")


def cmd_sell(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not args:
        return _reply("사용법: `/sell 종목코드` (예: `/sell 005930`)")
    code = args[0].strip()
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 계좌 조회 실패\n`{exc}`")
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))
    if code not in holdings:
        return _reply(f"`{code}` 종목은 갖고 있지 않습니다")
    p = holdings[code]
    text = (
        f"*판매 확인 필요*\n"
        f"{p['name']} (`{code}`)\n"
        f"수량: *{p['qty']}주*\n"
        f"평균 구매가 {int(p['avg_price']):,}원 · 지금 가격 {int(p['cur_price']):,}원\n"
        f"손익 {int(p['pnl']):+,}원 ({p['pnl_pct']:+.2f}%)\n\n"
        f"_아래 버튼을 누르면 전량 판매됩니다._"
    )
    return _reply(text, reply_markup=_sell_confirm_keyboard(code, p["name"], p["qty"]))


def cmd_cycle(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """점검 1회 즉시 실행. poller 스레드에서 동기 실행 (20~30초 소요)."""
    from trading_bot.signals.cycle import run_cycle

    with ctx.trading_lock:
        try:
            summary = run_cycle(ctx.settings, ctx.kis, ctx.llm, ctx.risk)
        except Exception as exc:
            log.exception("수동 점검 실행 실패")
            return _reply(f"❌ 점검 실행 중 오류\n`{exc}`")
    return _reply(
        f"*점검 완료*\n"
        f"후보 {summary.get('candidates', 0)} · "
        f"구매 {summary.get('buy', 0)} · 판매 {summary.get('sell', 0)} · 관망 {summary.get('hold', 0)}\n"
        f"주문 접수 {summary.get('orders_submitted', 0)} · 안전장치 차단 {summary.get('orders_rejected_by_risk', 0)}\n"
        f"자동 청산 {summary.get('exits_executed', 0)} · "
        f"AI 비용 ${summary.get('cost_usd', 0):.4f}"
    )


def cmd_update(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """봇 업데이트 조작 (2단계 확인 플로우).

    사용법:
      /update              — 현재/최신 버전 표시 + 업데이트 필요 여부 안내
      /update confirm      — 실제 업데이트 실행 (Watchtower 호출)
      /update enable       — 자동 업데이트 켜기
      /update disable      — 자동 업데이트 끄기
      /update status       — 자동 업데이트 상태 확인
    """
    if not args:
        return _check_update(ctx)

    sub = args[0].lower()
    if sub == "confirm":
        return _apply_update(ctx)
    if sub == "enable":
        update_manager.enable_auto()
        return _reply(
            "✅ *자동 업데이트 켜짐*\n"
            "매일 02:00 KST 에 새 버전이 있으면 자동으로 반영됩니다.\n"
            "지금 확인하려면 `/update` 입력."
        )
    if sub == "disable":
        update_manager.disable_auto(reason="telegram /update disable")
        return _reply(
            "🛑 *자동 업데이트 꺼짐*\n"
            "이제 02:00 KST 자동 업데이트가 스킵됩니다.\n"
            "수동 업데이트는 여전히 가능합니다 — `/update` 로 확인, "
            "`/update confirm` 으로 실행.\n"
            "다시 켜려면 `/update enable`."
        )
    if sub == "status":
        enabled = update_manager.is_auto_enabled()
        if enabled:
            return _reply(
                "*자동 업데이트 상태*\n"
                "• 현재: ✅ 켜짐\n"
                "• 스케줄: 매일 02:00 KST (장외 시간)\n"
                "• 수동 확인: `/update`\n"
                "• 수동 실행: `/update confirm`\n"
                "• 끄기: `/update disable`"
            )
        else:
            since = update_manager.disabled_since() or "(시각 불명)"
            return _reply(
                "*자동 업데이트 상태*\n"
                f"• 현재: 🛑 꺼짐\n"
                f"• 꺼진 시각: `{since}`\n"
                "• 수동 확인: `/update` (여전히 가능)\n"
                "• 수동 실행: `/update confirm`\n"
                "• 다시 켜기: `/update enable`"
            )

    return _reply(
        "*업데이트 명령어*\n"
        "`/update` — 현재/최신 버전 확인 (실행 안 함)\n"
        "`/update confirm` — 실제 업데이트 실행\n"
        "`/update enable` — 자동 업데이트 켜기\n"
        "`/update disable` — 자동 업데이트 끄기\n"
        "`/update status` — 자동 업데이트 상태 확인"
    )


def _check_update(ctx: BotContext) -> dict[str, Any]:
    """업데이트 확인만 하고 필요 여부를 안내. 실제 적용은 /update confirm."""
    # 현재 버전 (Docker 이미지에 주입된 BOT_VERSION)
    current_version = bot_version

    # 최신 릴리스 버전 (GitHub Releases API)
    latest_version: str | None = None
    latest_err: str | None = None
    try:
        latest_version = update_manager.fetch_latest_release_version()
    except Exception as exc:
        latest_err = str(exc)
        log.warning("최신 릴리스 버전 조회 실패: %s", exc)

    # digest 비교 — 실제 업데이트 필요 여부는 이걸로 판단
    has_update: bool | None = None
    try:
        has_update, _, _ = update_manager.check_for_update()
    except Exception as exc:
        log.warning("digest 비교 실패: %s", exc)

    lines = [
        "*업데이트 확인*",
        "",
        f"현재 버전: `{current_version}`",
        f"최신 버전: `{latest_version or '?'}`",
    ]
    if latest_err:
        lines.append(f"  _최신 릴리스 조회 실패: {latest_err[:80]}_")
    lines.append("")

    if has_update is False:
        lines.append("✅ *이미 최신 버전입니다*")
        lines.append("추가 업데이트가 필요하지 않습니다.")
    elif has_update is True:
        lines.append("🆕 *새 버전이 있습니다*")
        lines.append("")
        lines.append("적용하려면 아래 명령어를 입력하세요:")
        lines.append("`/update confirm`")
        lines.append("")
        lines.append("_Watchtower 가 새 이미지를 내려받고 봇을 재시작합니다._")
        lines.append("_소요 시간 약 30~60초, 이 과정에서 잠시 응답이 중단됩니다._")
    else:
        # digest 비교 실패 — 사용자가 직접 판단
        lines.append("❓ *업데이트 필요 여부 확인 불가*")
        lines.append("")
        lines.append("GHCR 연결에 문제가 있어 이미지 비교를 할 수 없습니다.")
        lines.append("강제로 업데이트를 시도하려면 `/update confirm` 입력.")

    return _reply("\n".join(lines))


def _apply_update(ctx: BotContext) -> dict[str, Any]:
    """/update confirm — digest 비교 후 필요할 때만 Watchtower 호출."""
    token = ctx.settings.watchtower_http_token
    current_version = bot_version

    # 선행 체크: 이미 최신이면 Watchtower 호출 자체를 건너뛴다.
    # 이렇게 해야 불필요한 Watchtower 알림("1 Scanned, 0 Updated") 이 안 뜨고
    # 봇의 응답 한 줄로 깔끔하게 마무리된다.
    try:
        has_update, _, _ = update_manager.check_for_update()
    except Exception as exc:
        log.warning("digest 비교 실패, Watchtower 에 맡김: %s", exc)
        has_update = True  # 확실치 않으면 일단 호출

    if not has_update:
        return _reply(f"✅ 현재 최신 버전입니다 (`{current_version}`)")

    # 실제 업데이트 필요 → Watchtower 호출
    latest_version: str | None = None
    try:
        latest_version = update_manager.fetch_latest_release_version()
    except Exception:
        pass

    try:
        update_manager.trigger_update(token)
    except Exception as exc:
        return _reply(f"❌ *업데이트 요청 실패*\n`{exc}`")

    lines = [
        "🔄 *업데이트 요청 전송됨*",
        "",
        f"현재 버전: `{current_version}`",
        f"최신 버전: `{latest_version or '?'}`",
        "",
        "Watchtower 가 새 이미지를 내려받고 봇을 재시작합니다.",
        "약 30~60초 후 *봇 기동* 메시지가 도착하면 완료입니다.",
    ]
    return _reply("\n".join(lines))


def cmd_about(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """봇 메타 정보 + 전체 설정 요약."""
    s = ctx.settings
    risk = s.risk or {}
    exit_cfg = s.exit_rules or {}
    llm_cfg = s.llm or {}
    prefilter_cfg = s.prefilter or {}

    uptime_s = (datetime.now() - ctx.started_at).total_seconds()
    uptime = fmt_uptime(uptime_s)

    badge = mode_badge(s.kis.mode)
    quote_server = "실전 서버" if s.kis_quote.mode == "live" else "모의 서버"
    kill_badge = "🛑 켜짐" if kill_switch.is_active() else "✅ 꺼짐"

    try:
        today_orders = repo.get_today_order_count()
    except Exception:
        today_orders = 0
    try:
        today_cost = repo.today_llm_cost_usd()
    except Exception:
        today_cost = 0.0

    # 임계값을 퍼센트 정수로 표시
    conf_thr_int = int(round(float(llm_cfg.get("confidence_threshold", 0.75)) * 100))

    lines = [
        f"*자동매매 봇 정보*",
        f"버전: `{bot_version}`",
        f"Python: `{platform.python_version()}` · {platform.system()}",
        f"가동 시간: {uptime}",
        f"기동 시각: {ctx.started_at:%Y-%m-%d %H:%M:%S}",
        "",
        f"*거래 설정* {badge}",
        f"• 시세 서버: {quote_server}",
        f"• 계좌: `{s.kis.account_no}-{s.kis.account_product_cd}`",
        f"• 관심 종목: {len(s.universe)}개",
        f"• 점검 주기: {s.cycle_minutes}분",
        f"• 장 시간: {s.market_open} ~ {s.market_close}",
        f"• 긴급 정지: {kill_badge}",
        "",
        f"*🤖 AI 판단*",
        f"• 모델: `{llm_cfg.get('model', 'claude-haiku-4-5')}`",
        f"• 확신도 임계값: {conf_thr_int}%",
        f"• 일일 AI 비용 한도: `${float(llm_cfg.get('daily_cost_limit_usd', 5)):.2f}`",
        f"• 오늘 사용: `${today_cost:.4f}`",
        "",
        f"*📋 1차 조건 (룰베이스 사전필터)*",
        f"• RSI 과매도 기준: < {prefilter_cfg.get('rsi_buy_below', 35)}",
        f"• RSI 과매수 기준: > {prefilter_cfg.get('rsi_sell_above', 70)}",
        f"• 거래량 최소 배수: {prefilter_cfg.get('min_volume_ratio', 1.2)}x (20일 평균 대비)",
        "",
        f"*🛡️ 안전장치 (리스크 매니저)*",
        f"• 종목당 비중 상한: {risk.get('max_position_per_symbol_pct', 15)}%",
        f"• 동시 보유 최대: {risk.get('max_concurrent_positions', 3)}종목",
        f"• 일일 손실 한도: -{risk.get('daily_loss_limit_pct', 3)}%",
        f"• 일일 주문 한도: {risk.get('max_orders_per_day', 6)}건 (오늘 {today_orders}건)",
        f"• 재거래 대기: {risk.get('cooldown_minutes', 60)}분",
        "",
        f"*💸 자동 청산*",
        f"• 🛡️ 손실 차단: -{exit_cfg.get('stop_loss_pct', 5)}%",
        f"• 🎯 이익 확정: +{exit_cfg.get('take_profit_pct', 15)}%",
        f"• 📉 트레일링 활성: +{exit_cfg.get('trailing_activation_pct', 7)}%",
        f"• 📉 트레일링 낙폭: -{exit_cfg.get('trailing_distance_pct', 4)}%",
        "",
        f"*🔄 자동 업데이트*",
        (
            f"• 상태: ✅ 켜짐 (매일 02:00 KST)"
            if update_manager.is_auto_enabled()
            else f"• 상태: 🛑 꺼짐 (`/update enable` 로 다시 켜기)"
        ),
        f"• 수동 실행: `/update`",
        "",
        f"*🔗 저장소*",
        f"• GitHub: github.com/hdream0322/trading",
        f"• 이미지: `ghcr.io/hdream0322/trading:latest`",
    ]
    return _reply("\n".join(lines))


COMMAND_MAP: dict[str, Callable[[BotContext, list[str]], dict[str, Any]]] = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/about": cmd_about,
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
    "/update": cmd_update,
}


def handle_command(ctx: BotContext, cmd: str, args: list[str]) -> dict[str, Any] | None:
    handler = COMMAND_MAP.get(cmd.lower())
    if handler is None:
        return _reply(f"모르는 명령어: `{cmd}`\n`/help` 로 목록 보기")
    try:
        return handler(ctx, args)
    except Exception as exc:
        log.exception("커맨드 %s 처리 중 예외", cmd)
        return _reply(f"❌ 처리 실패\n`{type(exc).__name__}: {exc}`")


# ─────────────────────────────────────────────────────────────
# 콜백 (inline 버튼) 핸들러
# ─────────────────────────────────────────────────────────────

def handle_callback(ctx: BotContext, data: str) -> dict[str, Any] | None:
    if data == "cancel":
        return _reply("취소됐습니다")
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
    return _reply(f"모르는 버튼: `{data}`")


def _execute_confirmed_sell(ctx: BotContext, code: str) -> dict[str, Any]:
    """판매 확정 버튼 처리. trading_lock으로 자동 점검과 직렬화."""
    with ctx.trading_lock:
        try:
            bal = ctx.kis.get_balance()
        except Exception as exc:
            return _reply(f"❌ 계좌 조회 실패\n`{exc}`")
        holdings = KisClient.normalize_holdings(bal.get("holdings", []))
        if code not in holdings:
            return _reply(f"`{code}` 종목은 이미 갖고 있지 않습니다")
        p = holdings[code]
        qty = int(p["qty"])
        try:
            result = ctx.kis.place_market_order(code, "sell", qty)
        except Exception as exc:
            repo.insert_error(component="manual_sell", message=f"{code} {qty}: {exc}")
            return _reply(f"❌ *판매 실패*\n{p['name']} ({code})\n`{exc}`")

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
        f"✅ *판매 주문 접수*\n"
        f"{p['name']} ({code})\n"
        f"{qty}주 지금 가격으로\n"
        f"주문번호 `{order_no}`"
    )
