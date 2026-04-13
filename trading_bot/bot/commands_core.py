"""핵심 조회/조작 커맨드 — help, menu, status, positions, signals, sell, cycle 등."""
from __future__ import annotations

import json
import logging
import platform
import sqlite3
from datetime import datetime
from typing import Any

from trading_bot import __version__ as bot_version
from trading_bot.bot import quiet_mode, update_manager
from trading_bot.bot.context import BotContext
from trading_bot.bot.formatters import (
    confidence_pct,
    decision_ko,
    fmt_pct,
    fmt_uptime,
    fmt_won,
    mode_badge,
)
from trading_bot.bot.keyboards import (
    _menu_keyboard,
    _positions_sell_keyboard,
    _reply,
    _sell_confirm_keyboard,
    _sell_picker_keyboard,
    cycle_summary_keyboard,
    kill_toggle_keyboard,
)
from trading_bot.kis.client import KisClient
from trading_bot.risk import kill_switch
from trading_bot.store import repo
from trading_bot.store.db import DB_PATH

log = logging.getLogger(__name__)


HELP_TEXT = """*자동매매 봇 사용법*

━━━━━━━━━━━━━━━━━━━━
*🏠 시작 · 허브*
━━━━━━━━━━━━━━━━━━━━
`/init` — 🚀 첫 설치 마법사 (처음 한 번, 버튼으로 단계별)
`/menu` — 메인 메뉴 (자주 쓰는 기능을 버튼으로)
`/help` — 이 도움말

━━━━━━━━━━━━━━━━━━━━
*📊 지금 상태 · 조회*
━━━━━━━━━━━━━━━━━━━━
`/status` — 총자산·예수금·긴급정지·오늘 비용
`/positions` — 갖고 있는 주식 (판매 버튼 포함)
`/signals` — 오늘 매매 추천 (최근 10개)
`/accuracy` — AI 판단 적중률 (사후 검증)
`/cost` — 오늘 AI 분석 비용
`/about` — 버전·업타임·전체 설정 요약

━━━━━━━━━━━━━━━━━━━━
*💸 수동 거래*
━━━━━━━━━━━━━━━━━━━━
`/sell` — 보유 종목 선택해 판매
`/sell 005930` — 코드 직접 지정 판매
`/cycle` — 지금 바로 점검 한 번 실행

━━━━━━━━━━━━━━━━━━━━
*🛑 안전장치*
━━━━━━━━━━━━━━━━━━━━
`/stop` — 🛑 긴급 정지 (새로 구매 차단)
`/resume` — ✅ 긴급 정지 풀기
`/quiet` — 🔕 조용 모드 (10분 요약 끔, 거래/에러는 그대로)

━━━━━━━━━━━━━━━━━━━━
*⚙️ 설정 변경*
━━━━━━━━━━━━━━━━━━━━
*거래 모드*
`/mode` — 현재 모드 + 전환 버튼
`/mode paper` / `/mode live confirm` — 직접 전환

*종목 관리*
`/universe` — 추적 종목 목록
`/universe add 005490` — 추가 (이름 확인 후 예/아니오)
`/universe remove` — 제거 (버튼 선택)
`/universe remove 005490` — 코드 직접 지정 제거

*수치 조정 (settings.yaml)*
`/config` — 설정 파일 진단 (누락 키 체크)
`/config raw` / `/config file` — 원본 텍스트/첨부
`/set` — 편집 가능 키 목록 + 현재값 + 범위
`/set risk.daily_loss_limit_pct 5` — 값 변경 (확정 버튼)

*펀더멘털 게이트*
`/funda 005930` — 종목 재무지표 (PER/PBR/ROE/부채비율)
`/funda enable` / `/funda disable` — 게이트 토글

━━━━━━━━━━━━━━━━━━━━
*📋 로그 · 데이터 내보내기*
━━━━━━━━━━━━━━━━━━━━
`/logs` — 최근 30줄
`/logs 100` — 최근 100줄 (길면 파일 전환)
`/logs error` — 최근 ERROR/WARNING 30건
`/logs file` — 오늘 로그 파일 다운로드
`/logs file 2026-04-12` — 특정 날짜 파일

`/export` — 📤 CSV 내보내기 메뉴
`/export signals` — 오늘 점검 전체 (RSI/거래량/판정)
`/export nearmiss` — 1차 통과 직전 TOP20
`/export orders 7` — 최근 7일 주문
`/export errors 3` — 최근 3일 에러
`/export db` — DB 파일 통째로 (50MB 미만)

━━━━━━━━━━━━━━━━━━━━
*🔄 업데이트 · 재시작*
━━━━━━━━━━━━━━━━━━━━
`/update` — 최신 버전 확인
`/update confirm` — 실제 업데이트 실행
`/update enable` / `/update disable` — 자동 업데이트 토글 (매일 02:00)
`/update status` — 자동 업데이트 상태

`/reload` — 자격증명 파일 재로드 (키 교체 후)
`/restart` — 컨테이너 완전 재시작 (강력 재초기화)

━━━━━━━━━━━━━━━━━━━━
*🔑 자격증명*
━━━━━━━━━━━━━━━━━━━━
`/setcreds` — 앱키/시크릿/계좌번호 교체 (3개월 갱신 시)
_(`/init` 안에서도 대화형 입력 가능)_

━━━━━━━━━━━━━━━━━━━━
_매 10분마다 봇이 관심 종목을 점검합니다._
_조건 충족 시 AI가 판단해 자동 주문._
_모든 매매는 안전장치 8단계를 거칩니다._
"""


def cmd_help(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    return _reply(HELP_TEXT)


def cmd_menu(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """자주 쓰는 기능을 버튼 하나로 모은 허브 화면."""
    kill_active = kill_switch.is_active()
    kill_line = "🛑 *긴급 정지 켜짐*" if kill_active else "✅ 정상 운영 중"
    badge = mode_badge(ctx.settings.kis.mode)
    universe_count = len(ctx.settings.universe)
    text = (
        f"*자동매매 봇* {badge}\n"
        f"{kill_line}\n"
        f"추적 중인 종목: {universe_count}개\n\n"
        f"_원하는 동작을 버튼으로 고르세요._"
    )
    return _reply(text, reply_markup=_menu_keyboard(kill_active))


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
    lines = [f"*갖고 있는 주식 {len(holdings)}개*"]
    for code, p in holdings.items():
        pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
        lines.append("")
        lines.append(f"{pnl_emoji} *{p['name']}* (`{code}`)")
        lines.append(
            f"   {int(p['qty'])}주 · 손익 {int(p['pnl']):+,}원 ({p['pnl_pct']:+.2f}%)"
        )
        lines.append(
            f"   평균 {int(p['avg_price']):,}원 → 지금 {int(p['cur_price']):,}원"
        )
        lines.append(f"   평가금액 {int(p['eval_amount']):,}원")
    lines.append("")
    lines.append("_판매하려면 아래 버튼을 누르세요._")
    return _reply("\n".join(lines), reply_markup=_positions_sell_keyboard(holdings))


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
        signal_summary = repo.get_today_signal_summary()
        risk_reasons = repo.get_today_risk_rejection_reasons()
    except Exception as exc:
        return _reply(f"❌ 기록 조회 실패\n`{exc}`")

    lines: list[str] = []
    if rows:
        lines.append("*오늘 매매 추천 (최근 10개)*")
        for t, code, name, decision, conf, reason in rows:
            conf_str = f" · 확신도 {confidence_pct(conf)}" if conf is not None else ""
            emoji = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}.get(decision, "❓")
            lines.append("")
            lines.append(
                f"{emoji} `{t}` *{name or code}* → {decision_ko(decision)}{conf_str}"
            )
            if reason:
                lines.append(f"   _{reason}_")
    else:
        lines.append("_오늘 나온 추천이 아직 없습니다._")

    # 사후 통계 — 점검/후보/판단/차단 요약
    if signal_summary["total_checks"] > 0:
        lines.append("")
        lines.append("*📊 오늘 요약*")
        lines.append(
            f"- 점검 {signal_summary['total_checks']}회 · 1차 통과 "
            f"{signal_summary['prefilter_pass']}개"
        )
        lines.append(
            f"- AI: 구매 {signal_summary['llm_buy']} · 판매 {signal_summary['llm_sell']} · "
            f"관망 {signal_summary['llm_hold']}"
        )
        if signal_summary["low_confidence"] > 0:
            lines.append(
                f"- 확신도 75% 미달로 주문까지 못 간 건 {signal_summary['low_confidence']}건"
            )

    if risk_reasons:
        lines.append("")
        lines.append("*⛔ 안전장치가 막은 사유*")
        for reason, count in risk_reasons:
            lines.append(f"- {reason}: {count}건")

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


def cmd_accuracy(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """AI 판단 사후 정확도 — confidence 구간별 적중률 + 교차검증 태그 집계.

    적중 정의: buy → 5 거래일 뒤 +1% 이상, sell → -1% 이하.
    """
    try:
        buckets = repo.get_accuracy_by_confidence_bucket()
        cross = repo.get_accuracy_by_cross_check()
    except Exception as exc:
        return _reply(f"❌ 정확도 조회 실패\n`{exc}`")

    total = sum(b["count"] for b in buckets)
    if total == 0:
        return _reply(
            "*AI 판단 적중률*\n\n"
            "_아직 평가된 판단이 없어요._\n"
            "_buy/sell 판단이 5 거래일 지나면 결과가 쌓입니다._"
        )

    lines = [
        "*AI 판단 적중률 (사후 검증)*",
        "_buy/sell 판단 5 거래일 뒤의 수익률 기반_",
        f"_총 평가 건수: {total}건_",
        "",
        "*확신도 구간별 적중률*",
    ]
    for b in buckets:
        if b["count"] == 0:
            lines.append(
                f"- `{int(b['low']*100)}~{int(b['high']*100)}%` 없음"
            )
            continue
        lines.append(
            f"- `{int(b['low']*100)}~{int(b['high']*100)}%` "
            f"{b['count']}건 · 적중 {b['hit_rate']:.0f}% · "
            f"평균 {b['avg_return']:+.2f}%"
        )

    conflict = cross.get("DIRECTION_CONFLICT", {})
    hold = cross.get("LLM_HOLD", {})
    if conflict.get("count", 0) > 0 or hold.get("count", 0) > 0:
        lines.append("")
        lines.append("*교차검증 불일치 집계*")
        if conflict.get("count", 0) > 0:
            lines.append(
                f"- 정반대 판단 ({conflict['count']}건) · "
                f"평균 {conflict['avg_return']:+.2f}%"
            )
        if hold.get("count", 0) > 0:
            lines.append(
                f"- LLM이 관망 선택 ({hold['count']}건) · "
                f"평균 {hold['avg_return']:+.2f}%"
            )
        lines.append(
            "_(prefilter 기준 수익률. 수치가 양수면 프리필터 방향이 맞았다는 뜻.)_"
        )

    lines.append("")
    lines.append("_적중률이 50% 근처면 AI 가 덜 맞추는 거예요._")
    lines.append("_그러면 settings.yaml 의 confidence_threshold 를 올려보세요._")
    return _reply("\n".join(lines))


def cmd_stop(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if kill_switch.is_active():
        return _reply("🛑 긴급 정지가 이미 켜져있습니다", reply_markup=kill_toggle_keyboard(True))
    kill_switch.activate(reason="telegram /stop command")
    return _reply(
        "🛑 *긴급 정지 켜짐*\n"
        "새로 구매는 막힙니다.\n"
        "이미 갖고 있는 주식은 필요 시 자동 판매가 계속됩니다 (손절/청산용).",
        reply_markup=kill_toggle_keyboard(True),
    )


def cmd_resume(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not kill_switch.is_active():
        return _reply("✅ 긴급 정지는 이미 꺼져있습니다", reply_markup=kill_toggle_keyboard(False))
    kill_switch.deactivate()
    return _reply(
        "✅ *긴급 정지 풀림*\n다음 점검부터 새로 구매가 가능합니다.",
        reply_markup=kill_toggle_keyboard(False),
    )


def cmd_quiet(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """조용 모드 토글 — 10분 사이클 요약 알림 on/off.

    - 인자 없음: 현재 상태 안내
    - `/quiet on`: 조용 모드 켜기 (hold-only 사이클 요약은 스킵,
                  거래/청산/차단/에러 있을 때만 알림)
    - `/quiet off`: 조용 모드 끄기 (10분마다 hold 여도 요약 전송 — 기본값)

    장 시작(09:00) · 장 마감(15:35) 브리핑은 조용 모드와 무관하게 항상 전송.
    """
    sub = (args[0].strip().lower() if args else "").strip()
    active = quiet_mode.is_active()

    if sub in ("on", "켜기", "활성"):
        if active:
            return _reply("🔕 이미 조용 모드입니다")
        quiet_mode.activate(reason="telegram /quiet on")
        return _reply(
            "🔕 *조용 모드 켜짐*\n"
            "10분마다 오던 사이클 요약을 끕니다.\n"
            "구매 / 판매 / 자동 청산 / 차단 / 에러가 있을 때만 알림이 옵니다.\n"
            "장 시작/마감 브리핑은 그대로 유지됩니다.\n\n"
            "`/quiet off` 로 다시 10분 요약을 받을 수 있습니다."
        )

    if sub in ("off", "끄기", "해제"):
        if not active:
            return _reply("🔔 이미 일반 모드입니다")
        quiet_mode.deactivate()
        return _reply(
            "🔔 *조용 모드 꺼짐*\n"
            "다시 10분마다 사이클 요약이 전송됩니다."
        )

    status_line = "🔕 *조용 모드 켜짐*" if active else "🔔 *일반 모드*"
    detail = (
        "10분 사이클 요약이 꺼져 있습니다.\n"
        "거래·청산·차단·에러가 있을 때만 알림이 옵니다.\n"
        "장 시작/마감 브리핑은 그대로 유지됩니다."
        if active
        else "10분마다 사이클 요약을 받고 있습니다.\n"
        "장 시작/마감 브리핑도 함께 유지됩니다."
    )
    from trading_bot.bot.keyboards import quiet_toggle_keyboard
    return _reply(
        f"{status_line}\n{detail}",
        reply_markup=quiet_toggle_keyboard(active),
    )


def cmd_sell(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """판매 흐름.

    - `/sell` (무인자): 보유 종목 목록을 버튼으로 띄움 → 탭 → 판매 확인
    - `/sell 005930`: 기존처럼 바로 판매 확인 화면
    """
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 계좌 조회 실패\n`{exc}`")
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))

    if not args:
        if not holdings:
            return _reply("갖고 있는 주식이 없습니다")
        lines = ["*판매할 종목을 고르세요*", ""]
        for code, p in holdings.items():
            pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
            lines.append(
                f"{pnl_emoji} *{p['name']}* (`{code}`) · "
                f"{int(p['qty'])}주 · 손익 {p['pnl_pct']:+.2f}%"
            )
        return _reply("\n".join(lines), reply_markup=_sell_picker_keyboard(holdings))

    code = args[0].strip()
    return _build_sell_confirm(holdings, code)


def _build_sell_confirm(
    holdings: dict[str, dict[str, Any]], code: str
) -> dict[str, Any]:
    """holdings 맵에서 code 를 찾아 판매 확인 화면을 만든다 (재사용 헬퍼)."""
    if code not in holdings:
        return _reply(f"`{code}` 종목은 갖고 있지 않습니다")
    p = holdings[code]
    text = (
        f"*판매 확인 필요*\n"
        f"{p['name']} (`{code}`)\n"
        f"수량: *{int(p['qty'])}주*\n"
        f"평균 구매가 {int(p['avg_price']):,}원 · 지금 가격 {int(p['cur_price']):,}원\n"
        f"손익 {int(p['pnl']):+,}원 ({p['pnl_pct']:+.2f}%)\n\n"
        f"_아래 버튼을 누르면 전량 판매됩니다._"
    )
    return _reply(text, reply_markup=_sell_confirm_keyboard(code, p["name"], int(p["qty"])))


def _sell_select(ctx: BotContext, code: str) -> dict[str, Any]:
    """콜백 sell_select: 선택된 코드 → 판매 확인 화면 (잔고 재조회)."""
    try:
        bal = ctx.kis.get_balance()
    except Exception as exc:
        return _reply(f"❌ 계좌 조회 실패\n`{exc}`")
    holdings = KisClient.normalize_holdings(bal.get("holdings", []))
    return _build_sell_confirm(holdings, code)


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

    # 접수 메시지를 먼저 보내고, 백그라운드로 체결 확인 + 텔레그램 알림.
    # 시장가라 장중엔 수 초 내 체결되므로 5초 대기 후 한 번 reconcile.
    # 미체결이면 다음 자동 사이클에서 어차피 잡힘.
    def _post_fill_check() -> None:
        import time as _t
        from trading_bot.signals import fill_tracker
        _t.sleep(5)
        try:
            with ctx.trading_lock:
                fill_tracker.reconcile_pending_orders(
                    ctx.kis,
                    auto_cancel_unfilled_buys=False,
                    telegram_cfg=ctx.settings.telegram,
                )
        except Exception:
            log.exception("수동 판매 체결 확인 실패")

    import threading
    threading.Thread(target=_post_fill_check, daemon=True).start()

    return _reply(
        f"✅ *판매 주문 접수*\n"
        f"{p['name']} ({code})\n"
        f"{qty}주 지금 가격으로\n"
        f"주문번호 `{order_no}`\n"
        f"_체결되면 알림 드릴게요_"
    )
