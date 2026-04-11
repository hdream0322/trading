from __future__ import annotations

import json
import logging
import platform
import sqlite3
import sys
from datetime import datetime
from typing import Any, Callable

from trading_bot import __version__ as bot_version
from trading_bot.bot import expiry, mode_switch, quiet_mode, runtime_state, update_manager
from trading_bot.bot.context import BotContext
from trading_bot.config import (
    CREDENTIALS_OVERRIDE_FILE,
    KisConfig,
    build_trade_cfg,
    load_credentials_override,
    save_universe_override,
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
    ("menu", "메인 메뉴 (버튼 허브)"),
    ("help", "사용법 보기"),
    ("status", "지금 상태 (자산·킬스위치·비용)"),
    ("positions", "갖고 있는 주식"),
    ("signals", "오늘 매매 추천 (최근 10개)"),
    ("accuracy", "AI 판단 적중률 (사후 검증)"),
    ("cost", "오늘 AI 분석 비용"),
    ("mode", "거래 모드 조회/전환 (실전/모의)"),
    ("universe", "추적 종목 목록/추가/제거"),
    ("about", "봇 버전 및 전체 설정"),
    ("stop", "🛑 긴급 정지 (새로 구매 차단)"),
    ("resume", "✅ 긴급 정지 풀기"),
    ("quiet", "🔕 조용 모드 토글 (10분 사이클 요약 끔)"),
    ("sell", "특정 종목 전부 팔기 (확인 필요)"),
    ("cycle", "지금 바로 점검 실행"),
    ("update", "최신 버전 확인"),
    ("setcreds", "텔레그램으로 앱키 직접 교체"),
    ("reload", "자격증명 재로드 (파일 수정 후)"),
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

def _reply(
    text: str,
    reply_markup: dict[str, Any] | None = None,
    delete_original: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {"text": text}
    if reply_markup is not None:
        out["reply_markup"] = reply_markup
    if delete_original:
        out["delete_original"] = True
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
            [
                {"text": "🔄 지금 점검", "callback_data": "cycle_run"},
            ],
        ]
    }


def _sell_picker_keyboard(holdings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """보유 종목을 각각 버튼으로 만들어 판매 대상 선택 화면."""
    rows: list[list[dict[str, str]]] = []
    for code, p in holdings.items():
        qty = int(p["qty"])
        pnl_pct = float(p["pnl_pct"])
        rows.append([{
            "text": f"{p['name']} {qty}주 ({pnl_pct:+.1f}%)",
            "callback_data": f"sell_select:{code}",
        }])
    rows.append([{"text": "❌ 취소", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def _positions_sell_keyboard(holdings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """/positions 응답 하단에 붙는 종목별 판매 버튼."""
    rows: list[list[dict[str, str]]] = []
    for code, p in holdings.items():
        rows.append([{
            "text": f"💸 {p['name']} 판매",
            "callback_data": f"sell_select:{code}",
        }])
    return {"inline_keyboard": rows}


def _universe_remove_picker_keyboard(universe: list[dict[str, str]]) -> dict[str, Any]:
    """/universe remove (인자 없음) → 추적 종목 각각을 제거 버튼으로."""
    rows: list[list[dict[str, str]]] = []
    for item in universe:
        rows.append([{
            "text": f"❌ {item['name']}",
            "callback_data": f"universe_rm_pick:{item['code']}",
        }])
    rows.append([{"text": "취소", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def _mode_switch_keyboard(current_mode: str) -> dict[str, Any]:
    """/mode (인자 없음) → 반대 모드로 전환 버튼."""
    if current_mode == "paper":
        btn = {"text": "🔴 실전으로 전환", "callback_data": "mode_to:live"}
    else:
        btn = {"text": "🟡 모의로 전환", "callback_data": "mode_to:paper"}
    return {"inline_keyboard": [[btn]]}


def _mode_live_confirm_keyboard() -> dict[str, Any]:
    """실전 전환 경고 화면에 붙는 확정/취소 버튼."""
    return {
        "inline_keyboard": [[
            {"text": "🚨 실전 전환 확정", "callback_data": "mode_confirm_live"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


def _menu_keyboard(kill_active: bool) -> dict[str, Any]:
    """/menu 허브 버튼."""
    third_row = (
        [{"text": "✅ 긴급정지 풀기", "callback_data": "resume"},
         {"text": "ℹ️ 사용법", "callback_data": "help"}]
        if kill_active
        else
        [{"text": "🛑 긴급 정지", "callback_data": "kill"},
         {"text": "ℹ️ 사용법", "callback_data": "help"}]
    )
    return {
        "inline_keyboard": [
            [
                {"text": "💰 상태", "callback_data": "status"},
                {"text": "📊 내 주식", "callback_data": "positions"},
            ],
            [
                {"text": "🌐 종목 목록", "callback_data": "universe_list"},
                {"text": "🔄 지금 점검", "callback_data": "cycle_run"},
            ],
            third_row,
        ]
    }


def _sell_confirm_keyboard(code: str, name: str, qty: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": f"✅ {name} {qty}주 판매 확정", "callback_data": f"sell_confirm:{code}"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


def _universe_confirm_keyboard(action: str, code: str) -> dict[str, Any]:
    """action: 'add' 또는 'remove'."""
    verb = "추가" if action == "add" else "제거"
    return {
        "inline_keyboard": [[
            {"text": f"✅ 예, {verb}할게요", "callback_data": f"universe_{action}:{code}"},
            {"text": "❌ 아니요", "callback_data": "cancel"},
        ]]
    }


# ─────────────────────────────────────────────────────────────
# 커맨드 핸들러
# ─────────────────────────────────────────────────────────────

HELP_TEXT = """*자동매매 봇 사용법*

*🏠 시작*
/menu — 메인 메뉴 (자주 쓰는 동작을 버튼으로)

*📊 조회*
/status — 지금 상태 (모드, 총 자산, 긴급정지, AI 비용)
/positions — 갖고 있는 주식 + 판매 버튼
/signals — 오늘 나온 매매 추천 (최근 10개)
/cost — 오늘 AI 분석 비용
/mode — 거래 모드 조회 (전환 버튼 있음)
/universe — 추적 중인 종목 목록
/universe add 005490 — 종목 추가 (이름 확인 후 예/아니오)
/universe remove — 종목 제거 (버튼으로 선택)
/universe remove 005490 — 코드 직접 지정 제거
/about — 봇 버전, 가동 시간, 전체 설정 요약

*⚙️ 조작*
/stop — 🛑 긴급 정지 (새로 구매 안 함)
/resume — ✅ 긴급 정지 풀기
/quiet — 🔕 조용 모드 (10분 사이클 요약 끔, 거래/에러는 그대로)
/sell — 보유 종목 목록에서 선택해 판매
/sell 005930 — 코드 직접 지정 판매
/cycle — 지금 바로 점검 한 번 돌리기

*🔄 업데이트*
/update — 최신 버전 확인 (현재/최신 버전 표시만)
/update confirm — 실제 업데이트 실행
/update enable — 자동 업데이트 켜기 (매일 02:00 KST)
/update disable — 자동 업데이트 끄기
/update status — 자동 업데이트 상태 확인

*🔑 자격증명 · 재시작*
/setcreds — 텔레그램으로 앱키 직접 교체 (3개월 갱신 시)
/reload — data/credentials.env 파일 재로드
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
            "정말로 실전 전환을 원하면 아래 버튼을 누르세요.",
            reply_markup=_mode_live_confirm_keyboard(),
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
        "_아래 버튼으로 다른 모드로 전환할 수 있어요._",
    ]
    return _reply("\n".join(lines), reply_markup=_mode_switch_keyboard(s.kis.mode))


def _swap_kis_mode(ctx: BotContext, new_mode: str, new_cfg: KisConfig) -> None:
    """런타임 KIS 클라이언트 교체.

    trading_lock 을 잡아서 진행 중인 사이클/수동 주문과 직렬화.
    성공 시 오버라이드 파일에 새 모드를 영속 저장.
    """
    with ctx.trading_lock:
        old_kis = ctx.kis
        new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
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
    """추적 종목 목록 조회 및 add/remove 서브커맨드.

    사용법:
      /universe                    — 현재 목록
      /universe add 005490         — 종목 추가 (KIS 로 이름 확인 후 예/아니오)
      /universe remove 005490      — 종목 제거 (예/아니오 확인)
    """
    if not args:
        return _universe_list(ctx)
    sub = args[0].lower()
    if sub == "add":
        if len(args) < 2:
            return _reply("사용법: `/universe add 005490`\n(종목코드 6자리)")
        return _universe_add_preview(ctx, args[1].strip())
    if sub in ("remove", "rm", "del", "delete"):
        if len(args) < 2:
            return _universe_remove_picker(ctx)
        return _universe_remove_preview(ctx, args[1].strip())
    return _reply(
        "사용법:\n"
        "`/universe` — 목록 보기\n"
        "`/universe add 005490` — 종목 추가\n"
        "`/universe remove 005490` — 종목 제거"
    )


def _universe_list(ctx: BotContext) -> dict[str, Any]:
    lines = ["*추적 중인 종목*"]
    if not ctx.settings.universe:
        lines.append("_(없음)_")
    else:
        for item in ctx.settings.universe:
            code = item["code"]
            name = item["name"]
            sector = str(item.get("sector", "")).strip()
            price_str = "?"
            try:
                price_output = ctx.kis.get_price(code)
                price = int(price_output.get("stck_prpr") or 0)
                if price > 0:
                    price_str = f"{price:,}원"
            except Exception:
                pass
            suffix = f" · _{sector}_" if sector else ""
            lines.append(f"- {name} (`{code}`) · {price_str}{suffix}")
    lines.append(f"\n점검 주기: {ctx.settings.cycle_minutes}분")
    lines.append(
        "\n추가: `/universe add 005490`\n"
        "제거: `/universe remove 005490`"
    )
    return _reply("\n".join(lines))


def _is_valid_stock_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def _universe_remove_picker(ctx: BotContext) -> dict[str, Any]:
    """/universe remove 인자 없음 → 추적 종목을 버튼으로 나열."""
    if not ctx.settings.universe:
        return _reply("추적 중인 종목이 없습니다")
    lines = ["*제거할 종목을 고르세요*", ""]
    for item in ctx.settings.universe:
        lines.append(f"- {item['name']} (`{item['code']}`)")
    return _reply(
        "\n".join(lines),
        reply_markup=_universe_remove_picker_keyboard(ctx.settings.universe),
    )


def _universe_add_preview(ctx: BotContext, code: str) -> dict[str, Any]:
    """종목코드를 받아 KIS 로 이름을 조회하고 예/아니오 확인 버튼을 띄운다."""
    if not _is_valid_stock_code(code):
        return _reply(
            f"❌ 잘못된 종목코드: `{code}`\n"
            f"6자리 숫자여야 합니다 (예: `005930`)"
        )
    for item in ctx.settings.universe:
        if item["code"] == code:
            return _reply(
                f"ℹ️ *{item['name']}* (`{code}`) 는 이미 추적 중입니다\n\n"
                f"`/universe` 로 전체 목록을 볼 수 있어요."
            )
    try:
        name = ctx.kis.get_stock_name(code)
    except Exception as exc:
        return _reply(
            f"❌ 종목 조회 실패\n`{exc}`\n\n"
            f"종목코드 `{code}` 가 정확한지 확인하세요."
        )
    price_line = ""
    try:
        price_output = ctx.kis.get_price(code)
        price = int(price_output.get("stck_prpr") or 0)
        if price > 0:
            price_line = f"\n지금 가격: `{price:,}원`"
    except Exception:
        pass
    text = (
        f"*종목 추가 확인*\n\n"
        f"종목명: *{name}*\n"
        f"종목코드: `{code}`"
        f"{price_line}\n\n"
        f"이 종목을 추적 목록에 추가할까요?"
    )
    return _reply(text, reply_markup=_universe_confirm_keyboard("add", code))


def _universe_remove_preview(ctx: BotContext, code: str) -> dict[str, Any]:
    if not _is_valid_stock_code(code):
        return _reply(
            f"❌ 잘못된 종목코드: `{code}`\n"
            f"6자리 숫자여야 합니다"
        )
    target: dict[str, str] | None = None
    for item in ctx.settings.universe:
        if item["code"] == code:
            target = item
            break
    if target is None:
        return _reply(
            f"❌ `{code}` 는 추적 목록에 없습니다\n"
            f"`/universe` 로 현재 목록을 확인하세요."
        )
    text = (
        f"*종목 제거 확인*\n\n"
        f"종목명: *{target['name']}*\n"
        f"종목코드: `{code}`\n\n"
        f"이 종목을 추적 목록에서 제거할까요?\n\n"
        f"_이미 갖고 있는 주식이라면 그대로 남으며,_\n"
        f"_자동 손절/익절 규칙은 계속 적용됩니다._"
    )
    return _reply(text, reply_markup=_universe_confirm_keyboard("remove", code))


def _execute_universe_add(ctx: BotContext, code: str) -> dict[str, Any]:
    """확인 버튼 탭 → 실제 추가. 중복/오류는 여기서도 방어.

    추가 시 KIS inquire-price 응답의 업종명(bstp_kor_isnm) 을 sector 필드로
    즉시 박아둔다 — 섹터 분산 게이트에서 바로 사용 가능.
    """
    for item in ctx.settings.universe:
        if item["code"] == code:
            return _reply(f"ℹ️ *{item['name']}* (`{code}`) 는 이미 추적 중입니다")
    try:
        name = ctx.kis.get_stock_name(code)
    except Exception as exc:
        return _reply(f"❌ 종목 조회 실패\n`{exc}`")
    sector = ctx.kis.get_stock_sector(code)
    entry: dict[str, str] = {"code": code, "name": name}
    if sector:
        entry["sector"] = sector
    new_universe = list(ctx.settings.universe) + [entry]
    try:
        save_universe_override(new_universe)
    except Exception as exc:
        log.exception("universe.json 저장 실패")
        return _reply(f"❌ 저장 실패\n`{exc}`")
    ctx.settings.universe = new_universe
    sector_line = f"\n업종: _{sector}_" if sector else ""
    return _reply(
        f"✅ *추적 목록에 추가됨*\n\n"
        f"*{name}* (`{code}`){sector_line}\n\n"
        f"이제 총 {len(new_universe)}개 종목을 추적합니다.\n"
        f"다음 점검부터 이 종목도 함께 봅니다."
    )


def _execute_universe_remove(ctx: BotContext, code: str) -> dict[str, Any]:
    target: dict[str, str] | None = None
    new_universe: list[dict[str, str]] = []
    for item in ctx.settings.universe:
        if item["code"] == code:
            target = item
        else:
            new_universe.append(item)
    if target is None:
        return _reply(f"❌ `{code}` 는 이미 추적 목록에 없습니다")
    try:
        save_universe_override(new_universe)
    except Exception as exc:
        log.exception("universe.json 저장 실패")
        return _reply(f"❌ 저장 실패\n`{exc}`")
    ctx.settings.universe = new_universe
    return _reply(
        f"✅ *추적 목록에서 제거됨*\n\n"
        f"*{target['name']}* (`{code}`)\n\n"
        f"이제 총 {len(new_universe)}개 종목을 추적합니다.\n"
        f"_이미 갖고 있는 주식이라면 자동 손절/익절은 계속 적용됩니다._"
    )


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
    lines = ["*갖고 있는 주식*"]
    for code, p in holdings.items():
        pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
        lines.append(
            f"{pnl_emoji} *{p['name']}* (`{code}`)\n"
            f"   {p['qty']}주 · 평균 구매가 {int(p['avg_price']):,}원 · 지금 가격 {int(p['cur_price']):,}원\n"
            f"   지금 평가 {int(p['eval_amount']):,}원 · 손익 {int(p['pnl']):+,}원 ({p['pnl_pct']:+.2f}%)"
        )
    lines.append("\n_판매하려면 아래 버튼을 누르세요._")
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
            conf_str = f" {confidence_pct(conf)}" if conf is not None else ""
            emoji = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}.get(decision, "❓")
            lines.append(f"{emoji} `{t}` {name or code} → *{decision_ko(decision)}*{conf_str}")
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
    return _reply(
        f"{status_line}\n{detail}\n\n"
        "`/quiet on` — 10분 요약 끄기 (거래 있을 때만 알림)\n"
        "`/quiet off` — 10분 요약 다시 켜기"
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


def cmd_update(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """봇 업데이트 조작.

    사용법:
      /update                 — 현재/최신 버전 표시 + 업데이트 필요 여부 안내
      /update confirm         — 최신 버전으로 업데이트 실행 (Watchtower 호출)
      /update notes           — 지금 버전의 릴리스 노트 보기
      /update notes 0.2.9     — 특정 버전의 릴리스 노트 보기
      /update enable          — 자동 업데이트 켜기
      /update disable         — 자동 업데이트 끄기
      /update status          — 자동 업데이트 상태 확인
    """
    if not args:
        return _check_update(ctx)

    sub = args[0].lower()
    if sub == "confirm":
        return _apply_update(ctx)
    if sub == "notes" or sub == "note":
        return cmd_notes(ctx, args[1:])
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
        "`/update` — 현재/최신 버전 확인\n"
        "`/update confirm` — 최신 버전으로 업데이트 실행\n"
        "`/update notes` — 지금 버전 릴리스 노트\n"
        "`/update notes 0.2.9` — 특정 버전 릴리스 노트\n"
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
        lines.append("✅ *이미 최신 버전이에요*")
        lines.append("지금은 업데이트할 게 없어요.")
    elif has_update is True:
        lines.append("🆕 *새 버전이 있어요*")
        lines.append("")
        lines.append("적용하려면 아래 명령어를 입력하세요:")
        lines.append("`/update confirm`")
        lines.append("")
        lines.append("_약 30~60초 뒤 봇이 자동으로 다시 시작돼요._")
        lines.append("_그동안 잠깐 응답이 멈출 수 있어요._")
    else:
        # digest 비교 실패 — 사용자가 직접 판단
        lines.append("❓ *업데이트 여부를 확인할 수 없어요*")
        lines.append("")
        lines.append("서버 연결에 문제가 있어서 버전 비교를 못했어요.")
        lines.append("그래도 업데이트를 시도하려면 `/update confirm` 을 입력하세요.")

    return _reply("\n".join(lines))


def _apply_update(ctx: BotContext) -> dict[str, Any]:
    """/update confirm — digest 비교 후 필요할 때만 Watchtower 호출."""
    token = ctx.settings.watchtower_http_token
    current_version = bot_version

    # 선행 체크: 이미 최신이면 Watchtower 호출 자체를 건너뛴다.
    try:
        has_update, _, _ = update_manager.check_for_update()
    except Exception as exc:
        log.warning("digest 비교 실패, Watchtower 에 맡김: %s", exc)
        has_update = True

    if not has_update:
        return _reply(f"✅ 현재 최신 버전입니다 (`{current_version}`)")

    # 릴리스 정보 (버전 + 태그 메시지) 조회 — 실패해도 업데이트는 진행
    info: dict[str, str] | None = None
    try:
        info = update_manager.fetch_latest_release_info()
    except Exception as exc:
        log.warning("릴리스 정보 조회 실패: %s", exc)

    try:
        update_manager.trigger_update(token)
    except Exception as exc:
        return _reply(f"❌ *업데이트 요청 실패*\n`{exc}`")

    latest_version = (info or {}).get("tag") or "?"
    lines = [
        "🔄 *업데이트 중...*",
        "_잠시만 기다려주세요_",
        "",
        f"📦 `{current_version}` → `{latest_version}`",
        "⏳ 약 30~60초 후 자동으로 재시작됩니다.",
    ]

    summary = _summarize_release_body((info or {}).get("body") or "")
    if summary:
        lines.append("")
        lines.append("📋 *이번 변경 사항*")
        lines.append("```")
        lines.append(summary)
        lines.append("```")

    return _reply("\n".join(lines))


def cmd_notes(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """현재 (또는 지정한) 버전의 릴리스 노트를 표시.

    사용법:
      /notes          — 지금 실행 중인 버전의 릴리스 노트
      /notes 0.2.9    — 특정 버전의 릴리스 노트 ('v' 접두사는 있어도/없어도 됨)

    내부적으로 GitHub Git Data API 로 annotated tag 의 message 를 가져온다.
    """
    if args:
        raw_version = args[0].strip().lstrip("vV")
    else:
        raw_version = bot_version

    # 로컬 개발 빌드 (예: '0.2.0-dev') 에는 대응되는 태그가 없다
    if not raw_version or "dev" in raw_version.lower() or "dirty" in raw_version.lower():
        return _reply(
            f"ℹ️ 현재 버전은 `{raw_version or '?'}` — 로컬 개발 빌드라 릴리스 노트가 없어요.\n"
            f"릴리스된 버전만 노트를 조회할 수 있습니다.\n\n"
            f"예: `/notes 0.2.10`"
        )

    tag_name = f"v{raw_version}"
    try:
        body = update_manager.fetch_tag_annotation(tag_name)
    except Exception as exc:
        log.warning("태그 annotation 조회 실패: %s", exc)
        return _reply(f"❌ 릴리스 노트 조회 실패\n`{exc}`")

    if not body:
        return _reply(
            f"ℹ️ `{tag_name}` 릴리스 노트를 찾을 수 없습니다.\n\n"
            f"GitHub 에서 직접 확인:\n"
            f"github.com/hdream0322/trading/releases/tag/{tag_name}"
        )

    summary = _summarize_release_body(body)
    if not summary:
        return _reply(f"ℹ️ `{tag_name}` 릴리스 노트 본문이 비어 있습니다.")

    header = (
        f"📋 *릴리스 노트* `{raw_version}`"
        if args
        else f"📋 *지금 버전 릴리스 노트* `{raw_version}`"
    )
    return _reply(f"{header}\n```\n{summary}\n```")


def _summarize_release_body(body: str, max_chars: int = 1500) -> str:
    """릴리스 바디/태그 메시지에서 사용자에게 보여줄 요약만 추출.

    - `---` 구분선 또는 `## Docker` / `## 배포` / `## NAS` 헤딩이 나오면 거기서 자름
    - 연속된 빈 줄은 하나로 압축
    - 트리플 백틱은 Telegram pre block 과 충돌하므로 작은따옴표 3개로 치환
    - `max_chars` 를 넘으면 뒤를 자르고 안내 문구 추가
    """
    if not body:
        return ""
    lines = body.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("## ") and (
            "Docker" in stripped or "배포" in stripped or "NAS" in stripped
        ):
            break
        # 커밋 메타데이터 (Co-Authored-By, Signed-off-by 등) 는 사용자에게 노이즈
        if stripped.lower().startswith(("co-authored-by:", "signed-off-by:")):
            continue
        kept.append(line)

    compact: list[str] = []
    prev_blank = False
    for line in kept:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        compact.append(line)
        prev_blank = blank

    text = "\n".join(compact).strip()
    text = text.replace("```", "'''")  # Telegram pre block 안전
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n… (전체 내용은 GitHub Release 페이지 참고)"
    return text


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
            "ℹ️ 아직 자격증명 오버라이드 파일이 없어요.\n\n"
            "지금 봇은 `.env` 에 있는 키로 정상 동작 중입니다. "
            "`/reload` 는 그 위에 덮어쓸 새 키가 있을 때만 의미가 있어요.\n\n"
            "*새 앱키로 갈아끼우려면* 텔레그램에서 바로 입력하세요:\n"
            "`/setcreds paper APP_KEY APP_SECRET ACCOUNT_NO`\n\n"
            "- 파일이 자동으로 생성되고 즉시 반영됩니다\n"
            "- 원본 메시지는 보안상 자동 삭제돼요\n"
            "- 실전 계좌는 끝에 `confirm` 을 붙이세요"
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
        new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
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


def cmd_setcreds(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """텔레그램으로 KIS 자격증명 직접 교체.

    사용법:
      /setcreds                                  — 사용법 표시
      /setcreds paper KEY SECRET ACCOUNT         — 모의 계좌 교체 (즉시)
      /setcreds live KEY SECRET ACCOUNT confirm  — 실전 계좌 교체 (confirm 필수)

    동작:
      1. 인자 검증
      2. data/credentials.env 에 병합 저장 (다른 모드 키는 보존)
      3. load_credentials_override() → build_trade_cfg → KisClient 교체
      4. 토큰 캐시 삭제 (해당 모드만)
      5. paper 모드면 expiry.mark_updated() (카운트다운 리셋)
      6. runtime_state.credentials_last_mtime 갱신 (watcher 중복 방지)
      7. 원본 /setcreds 메시지 삭제 플래그 반환 → poller 가 실제로 삭제

    보안:
      - chat_id 화이트리스트는 이미 poller 에서 적용됨
      - 시크릿 포함 메시지는 처리 직후 Telegram API 로 삭제
      - 로그에는 [REDACTED] 로 남김 (poller 쪽에서 처리)
    """
    if not args:
        return _reply(
            "*자격증명 직접 교체*\n\n"
            "*사용법*:\n"
            "`/setcreds paper <APP_KEY> <APP_SECRET> <계좌번호>`\n"
            "`/setcreds live <APP_KEY> <APP_SECRET> <계좌번호> confirm`\n\n"
            "*예시*:\n"
            "`/setcreds paper PSXXXyyy... longBase64String== 50181867`\n\n"
            "*주의*:\n"
            "- 모의(`paper`) 는 즉시 적용\n"
            "- 실전(`live`) 은 마지막에 `confirm` 필수\n"
            "- 시크릿이 포함된 메시지는 자동 삭제됩니다\n"
            "- 파일에 쓰는 방식(`nano ... credentials.env` + `/reload`) 도 여전히 사용 가능"
        )

    mode = args[0].lower()
    if mode not in ("paper", "live"):
        return _reply("첫 인자는 `paper` 또는 `live` 여야 합니다")

    if len(args) < 4:
        need = "KEY SECRET 계좌번호" + (" confirm" if mode == "live" else "")
        return _reply(
            f"인자 부족.\n"
            f"`/setcreds {mode} {need}` 형태로 입력하세요."
        )

    app_key = args[1]
    app_secret = args[2]
    account = args[3]

    # 실전은 confirm 필수
    if mode == "live":
        if len(args) < 5 or args[4].lower() != "confirm":
            return _reply(
                "🚨 *실전 계좌 자격증명 교체*\n\n"
                "실수 방지를 위해 마지막에 `confirm` 을 붙여야 합니다:\n"
                f"`/setcreds live <KEY> <SECRET> <계좌> confirm`\n\n"
                "실전 키는 실제 돈이 움직이는 계좌입니다. 신중하세요."
            )

    # 형식 검증
    if not (10 <= len(app_key) <= 64):
        return _reply(f"앱키 길이 이상: {len(app_key)}자 (보통 36자)")
    if not (40 <= len(app_secret) <= 256):
        return _reply(f"시크릿 길이 이상: {len(app_secret)}자 (보통 180자)")
    if not account.isdigit() or not (8 <= len(account) <= 10):
        return _reply(f"계좌번호 형식 이상: `{account}` (8자리 숫자 예상)")

    # 기존 credentials.env 읽어서 병합 (다른 모드 키 보존)
    existing: dict[str, str] = {}
    if CREDENTIALS_OVERRIDE_FILE.exists():
        try:
            for line in CREDENTIALS_OVERRIDE_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v
        except OSError as exc:
            return _reply(f"❌ 기존 credentials.env 읽기 실패\n`{exc}`")

    prefix = "KIS_LIVE" if mode == "live" else "KIS_PAPER"
    existing[f"{prefix}_APP_KEY"] = app_key
    existing[f"{prefix}_APP_SECRET"] = app_secret
    existing[f"{prefix}_ACCOUNT_NO"] = account
    # 상품코드는 기존 값 유지, 없으면 기본 01
    existing.setdefault(f"{prefix}_ACCOUNT_PRODUCT_CD", "01")

    # 파일 쓰기
    try:
        CREDENTIALS_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CREDENTIALS_OVERRIDE_FILE.open("w", encoding="utf-8") as f:
            f.write("# 텔레그램 /setcreds 또는 수동 편집으로 관리되는 자격증명 오버라이드\n")
            f.write("# 이 파일이 있으면 .env 의 KIS_* 값을 덮어씀\n")
            f.write("# 마지막 갱신: /setcreds 또는 nano 편집\n\n")
            for k in sorted(existing.keys()):
                f.write(f"{k}={existing[k]}\n")
        try:
            CREDENTIALS_OVERRIDE_FILE.chmod(0o600)
        except OSError:
            pass
        # watcher 가 중복 반응하지 않도록 mtime 기록 선반영
        try:
            runtime_state.credentials_last_mtime = CREDENTIALS_OVERRIDE_FILE.stat().st_mtime
        except OSError:
            pass
    except OSError as exc:
        return _reply(f"❌ credentials.env 파일 쓰기 실패\n`{exc}`")

    # 런타임 반영
    applied_now = False
    try:
        load_credentials_override()
        # 현재 활성 모드와 일치할 때만 KisClient 교체
        if ctx.settings.kis.mode == mode:
            new_cfg = build_trade_cfg(mode)
            with ctx.trading_lock:
                old_kis = ctx.kis
                new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
                ctx.settings.kis = new_cfg
                ctx.kis = new_kis
                try:
                    old_kis.close()
                except Exception:
                    pass

            # 해당 모드 토큰 캐시만 삭제
            from pathlib import Path
            tokens_dir = Path(__file__).resolve().parent.parent.parent / "tokens"
            try:
                token_file = tokens_dir / f"kis_token_{mode}.json"
                if token_file.exists():
                    token_file.unlink()
            except OSError:
                pass

            if mode == "paper":
                expiry.mark_updated()
            applied_now = True
    except Exception as exc:
        log.exception("setcreds 런타임 반영 실패")
        return _reply(
            f"⚠️ 파일 저장은 됐지만 런타임 반영 실패\n"
            f"`{exc}`\n\n"
            f"/reload 로 재시도 가능.",
            delete_original=True,
        )

    badge = mode_badge(mode)
    if applied_now:
        status_line = "✅ *즉시 반영됨*"
    else:
        status_line = (
            f"💾 파일에 저장됨 (현재 활성 모드는 `{ctx.settings.kis.mode}` 이라 이 값은 대기)"
        )

    extra = ""
    if mode == "paper" and applied_now:
        extra = f"\n만료 카운트다운: {expiry.PAPER_EXPIRY_DAYS}일 리셋됨"

    return _reply(
        f"✅ *자격증명 교체 완료* {badge}\n\n"
        f"계좌: `{account}`\n"
        f"앱키 앞 12자: `{app_key[:12]}...`\n"
        f"{status_line}{extra}\n\n"
        f"_원본 /setcreds 메시지는 자동 삭제됩니다._\n"
        f"`/status` 로 동작 확인.",
        delete_original=True,
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
    "/start": cmd_menu,
    "/help": cmd_help,
    "/menu": cmd_menu,
    "/about": cmd_about,
    "/mode": cmd_mode,
    "/universe": cmd_universe,
    "/status": cmd_status,
    "/positions": cmd_positions,
    "/signals": cmd_signals,
    "/cost": cmd_cost,
    "/accuracy": cmd_accuracy,
    "/stop": cmd_stop,
    "/kill": cmd_stop,
    "/resume": cmd_resume,
    "/quiet": cmd_quiet,
    "/sell": cmd_sell,
    "/cycle": cmd_cycle,
    "/update": cmd_update,
    "/setcreds": cmd_setcreds,
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
    if data == "help":
        return cmd_help(ctx, [])
    if data == "universe_list":
        return cmd_universe(ctx, [])
    if data == "cycle_run":
        return cmd_cycle(ctx, [])
    if data.startswith("sell_select:"):
        code = data.split(":", 1)[1]
        return _sell_select(ctx, code)
    if data.startswith("sell_confirm:"):
        code = data.split(":", 1)[1]
        return _execute_confirmed_sell(ctx, code)
    if data.startswith("universe_add:"):
        code = data.split(":", 1)[1]
        return _execute_universe_add(ctx, code)
    if data.startswith("universe_remove:"):
        code = data.split(":", 1)[1]
        return _execute_universe_remove(ctx, code)
    if data.startswith("universe_rm_pick:"):
        code = data.split(":", 1)[1]
        return _universe_remove_preview(ctx, code)
    if data.startswith("mode_to:"):
        target = data.split(":", 1)[1]
        return cmd_mode(ctx, [target])
    if data == "mode_confirm_live":
        return cmd_mode(ctx, ["live", "confirm"])
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
