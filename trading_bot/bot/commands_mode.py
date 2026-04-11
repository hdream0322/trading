"""/mode 커맨드 — 거래 모드(paper/live) 조회 및 런타임 전환."""
from __future__ import annotations

import logging
from typing import Any

from trading_bot.bot import mode_switch
from trading_bot.bot.context import BotContext
from trading_bot.bot.formatters import mode_badge
from trading_bot.bot.keyboards import (
    _mode_live_confirm_keyboard,
    _mode_switch_keyboard,
    _reply,
)
from trading_bot.config import KisConfig, build_trade_cfg
from trading_bot.kis.client import KisClient

log = logging.getLogger(__name__)


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
