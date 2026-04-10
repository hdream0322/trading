from __future__ import annotations

import json
import logging
import platform
import sqlite3
import sys
from datetime import datetime
from typing import Any, Callable

from trading_bot import __version__ as bot_version
from trading_bot.bot import expiry, mode_switch, update_manager
from trading_bot.bot.context import BotContext
from trading_bot.config import (
    CREDENTIALS_OVERRIDE_FILE,
    KisConfig,
    build_trade_cfg,
    load_credentials_override,
)
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
    ("mode", "거래 모드 조회/전환 (실전/모의)"),
    ("universe", "추적 중인 종목 목록"),
    ("about", "봇 버전 및 전체 설정"),
    ("stop", "🛑 긴급 정지 (새로 구매 차단)"),
    ("resume", "✅ 긴급 정지 풀기"),
    ("sell", "특정 종목 전부 팔기 (확인 필요)"),
    ("cycle", "지금 바로 점검 실행"),
    ("update", "최신 버전 확인"),
    ("reload", "자격증명 재로드 (앱키 교체)"),
    ("restart", "컨테이너 완전 재시작"),
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
/mode — 거래 모드 조회 및 전환 (실전/모의)
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

*🔑 자격증명 · 재시작*
/reload — data/credentials.env 재로드 (3개월 키 갱신 시)
/restart — 컨테이너 완전 재시작 (강력 재초기화)

/help — 이 도움말

---
_매 10분마다 봇이 관심 종목들을 점검합니다._
_조건이 맞으면 AI가 판단해서 자동으로 주문을 넣습니다._
_모든 매매는 안전장치 7단계를 거쳐야 실행됩니다._
"""


def cmd_help(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    return _reply(HELP_TEXT)


def cmd_mode(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """현재 모드 조회 및 전환.

    사용법:
      /mode              — 현재 모드 정보 표시
      /mode paper        — 모의 모드로 전환 (안전, 즉시 적용)
      /mode live         — 실전 모드 전환 경고 (confirm 필요)
      /mode live confirm — 실전 모드로 실제 전환 (실제 돈 움직임)
    """
    if not args:
        return _show_current_mode(ctx)

    target = args[0].lower()
    if target not in ("paper", "live"):
        return _reply(
            "사용법:\n"
            "`/mode` — 현재 모드 표시\n"
            "`/mode paper` — 모의 모드로 전환\n"
            "`/mode live` — 실전 모드 전환 (경고 후 confirm 필요)"
        )

    current = ctx.settings.kis.mode
    if target == current:
        return _reply(f"이미 `{target}` 모드입니다 — 변경 없음")

    # 대상 모드의 키가 .env 에 있는지 선행 검증
    try:
        new_cfg = build_trade_cfg(target)
    except RuntimeError as exc:
        prefix = "KIS_LIVE" if target == "live" else "KIS_PAPER"
        return _reply(
            f"❌ `{target}` 모드 키가 .env 에 없습니다\n"
            f"`{exc}`\n\n"
            f"필요한 환경변수:\n"
            f"• `{prefix}_APP_KEY`\n"
            f"• `{prefix}_APP_SECRET`\n"
            f"• `{prefix}_ACCOUNT_NO`"
        )

    confirm = len(args) > 1 and args[1].lower() == "confirm"

    # paper → live: 명시적 confirm 필수 (실제 돈 움직임)
    if target == "live" and not confirm:
        # 현재 보유 종목 개수 (paper) 파악해서 경고 강화
        try:
            bal = ctx.kis.get_balance()
            holdings = KisClient.normalize_holdings(bal.get("holdings", []))
            pos_count = len(holdings)
        except Exception:
            pos_count = -1

        pos_note = ""
        if pos_count > 0:
            pos_note = (
                f"\n⚠️ 현재 모의 계좌에 *{pos_count}개 종목* 보유 중.\n"
                f"전환 시 이 종목들은 모의 계좌에 그대로 남고 봇이 더 이상 관리하지 않습니다.\n"
            )

        return _reply(
            "🚨 *실전 모드 전환 경고*\n\n"
            f"현재: 🟡 모의 → 🔴 *실전*\n"
            f"대상 계좌: `{new_cfg.account_no}-{new_cfg.account_product_cd}`\n"
            f"{pos_note}\n"
            "*실전 모드의 의미*:\n"
            "- 다음 점검부터 *실제 돈* 이 움직입니다\n"
            "- 손실은 실제 손실입니다 (복구 불가)\n"
            "- 리스크 매니저가 보호하지만 완벽하지 않습니다\n"
            "- 장 시간(평일 09:00~15:30) 중에는 즉시 영향\n\n"
            "정말로 실전 전환을 원하면 아래를 입력하세요:\n"
            "`/mode live confirm`\n\n"
            "_모의로 돌아가려면 언제든 `/mode paper`._"
        )

    # 실제 전환 수행
    try:
        _swap_kis_mode(ctx, target, new_cfg)
    except Exception as exc:
        log.exception("mode 전환 실패")
        return _reply(f"❌ 모드 전환 실패\n`{exc}`")

    badge = mode_badge(target)
    mode_desc = (
        "*실제 돈이 움직입니다*" if target == "live"
        else "가상 거래 — 실제 돈 움직이지 않음"
    )
    extra = ""
    if target == "live":
        extra = (
            "\n\n⚠️ 지금부터 *실전* 모드입니다. 다음 점검부터 실제 주문이 들어갑니다.\n"
            "• 불안하면 즉시 `/stop` 으로 긴급 정지 가능\n"
            "• 되돌리려면 `/mode paper`"
        )
    return _reply(
        f"✅ *모드 전환 완료*\n\n"
        f"거래: {badge}\n"
        f"   _{mode_desc}_\n"
        f"계좌: `{new_cfg.account_no}-{new_cfg.account_product_cd}`\n"
        f"시세 조회: 실전 서버 (변경 없음)\n\n"
        f"다음 점검부터 이 모드로 동작합니다.{extra}"
    )


def _show_current_mode(ctx: BotContext) -> dict[str, Any]:
    s = ctx.settings
    badge = mode_badge(s.kis.mode)
    mode_desc = (
        "*실제 돈이 움직입니다*" if s.kis.mode == "live"
        else "가상 거래 — 실제 돈 움직이지 않음"
    )
    quote_server = "실전 서버" if s.kis_quote.mode == "live" else "모의 서버"
    lines = [
        "*지금 모드*",
        f"거래: {badge}",
        f"   _{mode_desc}_",
        f"시세 조회: {quote_server}",
        f"계좌: `{s.kis.account_no}-{s.kis.account_product_cd}`",
        "",
        "*전환*",
        "`/mode paper` — 모의로 (즉시)",
        "`/mode live` — 실전 전환 경고 → `/mode live confirm`",
    ]
    return _reply("\n".join(lines))


def _swap_kis_mode(ctx: BotContext, new_mode: str, new_cfg: KisConfig) -> None:
    """런타임 KIS 클라이언트 교체.

    trading_lock 을 잡아서 진행 중인 사이클/수동 주문과 직렬화.
    성공 시 오버라이드 파일에 새 모드를 영속 저장.
    """
    with ctx.trading_lock:
        old_kis = ctx.kis
        new_kis = KisClient(new_cfg, ctx.settings.kis_quote)
        # BotContext.settings 는 frozen 아님 → mutate 가능
        ctx.settings.kis = new_cfg
        ctx.kis = new_kis
        try:
            old_kis.close()
        except Exception:
            log.warning("이전 KisClient 종료 중 예외 (무시)", exc_info=True)

    # 락 해제 후 상태 파일 저장 (IO 가 락 구간 밖에 있어도 안전)
    mode_switch.write_override(new_mode)


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


def cmd_reload(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """data/credentials.env 파일을 재로드하고 KisClient 를 재생성.

    3개월 주기로 KIS 모의투자 앱키를 재발급 받아야 할 때 사용.
    Docker 재시작 없이 런타임에 새 자격증명 적용.

    사용자 흐름:
      1. KIS 에서 새 앱키/시크릿/계좌번호 발급
      2. NAS SSH: nano /volume1/docker/trading/data/credentials.env
      3. 파일에 새 값 작성 (KIS_PAPER_APP_KEY=... 등) 후 저장
      4. 텔레그램 /reload — 즉시 반영
    """
    if not CREDENTIALS_OVERRIDE_FILE.exists():
        return _reply(
            "⚠️ *credentials.env 파일 없음*\n\n"
            f"다음 경로에 파일을 먼저 생성하세요:\n"
            f"`data/credentials.env`\n\n"
            f"*파일 내용 예시*:\n"
            "```\n"
            "KIS_PAPER_APP_KEY=PSXXXxxxxXXXXxxxXXXX\n"
            "KIS_PAPER_APP_SECRET=긴문자열...\n"
            "KIS_PAPER_ACCOUNT_NO=12345678\n"
            "KIS_LIVE_APP_KEY=PSYYYyyyyYYYYyyyYYYY\n"
            "KIS_LIVE_APP_SECRET=긴문자열...\n"
            "KIS_LIVE_ACCOUNT_NO=87654321\n"
            "```\n\n"
            "NAS SSH 에서:\n"
            "`nano /volume1/docker/trading/data/credentials.env`\n"
            "`chmod 600 /volume1/docker/trading/data/credentials.env`\n\n"
            "파일 생성 후 `/reload` 다시 입력."
        )

    try:
        load_credentials_override()
        new_cfg = build_trade_cfg(ctx.settings.kis.mode)
    except Exception as exc:
        log.exception("자격증명 재로드 실패")
        return _reply(
            f"❌ *자격증명 재로드 실패*\n`{exc}`\n\n"
            f"credentials.env 내용 확인 후 다시 시도하세요."
        )

    # 토큰 캐시 삭제 — 옛 키로 발급된 토큰은 새 키와 안 맞음
    from pathlib import Path
    tokens_dir = Path(__file__).resolve().parent.parent.parent / "tokens"
    deleted_tokens = 0
    try:
        for token_file in tokens_dir.glob("kis_token_*.json"):
            token_file.unlink()
            deleted_tokens += 1
    except Exception as exc:
        log.warning("토큰 캐시 삭제 실패: %s", exc)

    # KisClient 원자적 교체 (trading_lock 으로 사이클과 직렬화)
    with ctx.trading_lock:
        old_kis = ctx.kis
        new_kis = KisClient(new_cfg, ctx.settings.kis_quote)
        ctx.settings.kis = new_cfg
        ctx.kis = new_kis
        try:
            old_kis.close()
        except Exception:
            pass

    # paper 모드 자격증명 재로드 시 만료 카운트다운 리셋
    if new_cfg.mode == "paper":
        expiry.mark_updated()

    badge = mode_badge(new_cfg.mode)
    expiry_line = ""
    if new_cfg.mode == "paper":
        expiry_line = f"\n만료 카운트다운: {expiry.PAPER_EXPIRY_DAYS}일 리셋됨"
    return _reply(
        f"✅ *자격증명 재로드 완료*\n\n"
        f"모드: {badge}\n"
        f"계좌: `{new_cfg.account_no}-{new_cfg.account_product_cd}`\n"
        f"앱키 앞 12자: `{new_cfg.app_key[:12]}...`\n"
        f"토큰 캐시 삭제: {deleted_tokens}개{expiry_line}\n\n"
        f"다음 KIS 호출 시 새 키로 새 토큰 자동 발급됩니다.\n"
        f"`/status` 로 동작 확인."
    )


def cmd_restart(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """컨테이너 완전 재시작.

    /reload 와 다른 점: /reload 는 Python 프로세스 내부에서 자격증명만 교체하지만,
    /restart 는 Python 프로세스 자체를 종료한다. docker-compose 의
    restart: unless-stopped 정책에 의해 Docker 가 자동으로 새 컨테이너를 띄운다.

    동작:
      1. 텔레그램에 '재시작 시작' 응답 전송 (return)
      2. 응답 전송 여유를 위해 2초 대기 후 SIGTERM 전송 (백그라운드 스레드)
      3. SIGTERM → main.py 의 _shutdown 핸들러 → scheduler.shutdown →
         sys.exit(0) → Docker 가 컨테이너 재생성
      4. 새 컨테이너가 기동되며 '봇 기동' 메시지 발송

    총 소요 시간: 약 10~20초 (이미지 다운로드 없이 순수 재시작).
    """
    import os
    import signal
    import threading
    import time

    def _delayed_kill() -> None:
        time.sleep(2)  # 응답 메시지가 먼저 전송되도록 잠시 대기
        log.warning("/restart 요청에 의한 SIGTERM 전송")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_delayed_kill, daemon=True).start()

    return _reply(
        "🔄 *컨테이너 재시작 요청*\n\n"
        "2초 후 봇 프로세스가 종료되고 Docker 가 새로 띄웁니다.\n"
        "약 10~20초 후 *봇 기동* 메시지가 도착하면 완료입니다.\n\n"
        "_이 동안 텔레그램 커맨드는 일시적으로 응답하지 않습니다._"
    )


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
    "/reload": cmd_reload,
    "/restart": cmd_restart,
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
