"""/style 커맨드 — 거래 스타일 프리셋(default/scalp/swing) 조회 및 전환.

Stage 11. settings.yaml `trade_modes:` 섹션의 프리셋을 prefilter/risk/exit/llm 에
인메모리로 오버레이. 활성 스타일은 `data/trade_mode` 파일에 영속화돼 컨테이너
재시작·이미지 업데이트에도 유지된다. 보유 종목 청산 룰도 즉시 새 폭 적용.
"""
from __future__ import annotations

import logging
from typing import Any

from trading_bot.bot import style_switch
from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply
from trading_bot.config import load_settings
from trading_bot.signals.exit_strategy import round_trip_cost_pct

log = logging.getLogger(__name__)

# /status, /config 등에서 한 줄로 노출할 때 사용하는 라벨.
_STYLE_LABELS = {
    "default": "🟢 기본",
    "scalp": "⚡ 단타",
    "swing": "🐢 장기",
}


def style_label(style: str) -> str:
    """외부 모듈에서 활성 스타일 라벨을 가져갈 때 호출."""
    return _STYLE_LABELS.get(style, style)


def cmd_style(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """사용법:
      /style                — 현재 스타일 + 프리셋 비교표 + 버튼
      /style scalp          — 단타 모드로 즉시 전환
      /style swing          — 장기 모드로 즉시 전환
      /style default        — 기본 모드 복귀 (settings.yaml 원본값)
    """
    if not args:
        return _show_current(ctx)

    target = args[0].lower()
    if target not in style_switch.VALID_STYLES:
        return _reply(
            "사용법:\n"
            "`/style` — 현재 스타일 + 비교\n"
            "`/style scalp` — 단타 (짧은 손익절·잦은 진입)\n"
            "`/style swing` — 장기 (보수적·긴 보유)\n"
            "`/style default` — 기본값 복귀"
        )

    current = ctx.settings.trade_style
    if target == current:
        return _reply(f"이미 *{style_label(target)}* 모드예요 — 변경 없음")

    try:
        _swap_style(ctx, target)
    except Exception as exc:
        log.exception("style 전환 실패")
        return _reply(f"❌ 스타일 전환 실패\n`{exc}`")

    return _reply(_swap_summary(ctx, current, target))


# ─────────────────────────────────────────────────────────────
# 표시 — 현재 스타일 + 비교표
# ─────────────────────────────────────────────────────────────

def _show_current(ctx: BotContext) -> dict[str, Any]:
    s = ctx.settings
    cur = s.trade_style
    fees = getattr(s, "fees", None) or {}
    rt = round_trip_cost_pct(fees)
    min_net = float(fees.get("min_net_profit_pct", 0.0)) if fees else 0.0
    tp = float(s.exit_rules.get("take_profit_pct") or 0)
    sl = float(s.exit_rules.get("stop_loss_pct") or 0)
    ta = float(s.exit_rules.get("trailing_activation_pct") or 0)
    td = float(s.exit_rules.get("trailing_distance_pct") or 0)
    worst_trail_gross = ta - td  # 트레일링 청산 최악 시나리오 (활성 직후 낙폭)
    lines = [
        f"*거래 스타일* — 지금: {style_label(cur)}",
        "",
        "*지금 적용 중인 값*",
        f"• RSI 매수/매도: < `{s.prefilter.get('rsi_buy_below')}` / > `{s.prefilter.get('rsi_sell_above')}`",
        f"• 거래량 최소: `{s.prefilter.get('min_volume_ratio')}x`",
        f"• 추세 필터: `{'켜짐' if s.prefilter.get('trend_filter_enabled') else '꺼짐'}`",
        f"• 확신도: `{s.llm.get('confidence_threshold')}`",
        f"• 재거래 대기: `{s.risk.get('cooldown_minutes')}분`",
        f"• 일일 주문 한도: `{s.risk.get('max_orders_per_day')}건`",
        f"• 손절/익절: `{sl}%` / `{tp}%` (수수료 후 `{sl + rt:.2f}%` / `{tp - rt:.2f}%`)",
        f"• 트레일링 활성/낙폭: `{ta}%` / `{td}%` "
        f"(최악 +{worst_trail_gross:.1f}%, 수수료 후 +{worst_trail_gross - rt:.2f}%)",
        f"• 왕복 수수료: `{rt:.2f}%` · 트레일링 가드 min net `{min_net}%`",
        "",
        "*프리셋 비교*",
        "```",
        _comparison_table(s.trade_modes),
        "```",
        "",
        "_전환: `/style scalp` · `/style swing` · `/style default`_",
    ]
    return _reply("\n".join(lines), reply_markup=_style_buttons(cur))


def _comparison_table(trade_modes: dict[str, Any]) -> str:
    """단타 / 장기 프리셋의 핵심 키만 한눈에. settings.yaml trade_modes 가
    비어 있으면 안내 문구로 폴백."""
    scalp = trade_modes.get("scalp") or {}
    swing = trade_modes.get("swing") or {}
    if not (scalp or swing):
        return "trade_modes 프리셋이 settings.yaml 에 정의되지 않음"

    def _g(p: dict, section: str, key: str, fallback: str = "-") -> str:
        v = ((p.get(section) or {}).get(key))
        return str(v) if v is not None else fallback

    rows = [
        ("RSI buy<",     _g(scalp, "prefilter", "rsi_buy_below"), _g(swing, "prefilter", "rsi_buy_below")),
        ("RSI sell>",    _g(scalp, "prefilter", "rsi_sell_above"), _g(swing, "prefilter", "rsi_sell_above")),
        ("vol min",      _g(scalp, "prefilter", "min_volume_ratio"), _g(swing, "prefilter", "min_volume_ratio")),
        ("trend",        _g(scalp, "prefilter", "trend_filter_enabled"), _g(swing, "prefilter", "trend_filter_enabled")),
        ("conf",         _g(scalp, "llm", "confidence_threshold"), _g(swing, "llm", "confidence_threshold")),
        ("cooldown",     _g(scalp, "risk", "cooldown_minutes"), _g(swing, "risk", "cooldown_minutes")),
        ("orders/day",   _g(scalp, "risk", "max_orders_per_day"), _g(swing, "risk", "max_orders_per_day")),
        ("SL %",         _g(scalp, "exit", "stop_loss_pct"), _g(swing, "exit", "stop_loss_pct")),
        ("TP %",         _g(scalp, "exit", "take_profit_pct"), _g(swing, "exit", "take_profit_pct")),
        ("trail act",    _g(scalp, "exit", "trailing_activation_pct"), _g(swing, "exit", "trailing_activation_pct")),
        ("trail dist",   _g(scalp, "exit", "trailing_distance_pct"), _g(swing, "exit", "trailing_distance_pct")),
    ]
    header = f"{'key':<11} {'scalp':>7} {'swing':>7}"
    body = "\n".join(f"{k:<11} {a:>7} {b:>7}" for k, a, b in rows)
    return f"{header}\n{body}"


def _style_buttons(current: str) -> dict[str, Any]:
    btns = []
    for style in ("default", "scalp", "swing"):
        mark = "✅ " if style == current else ""
        btns.append({
            "text": f"{mark}{style_label(style)}",
            "callback_data": f"style_to:{style}",
        })
    return {"inline_keyboard": [btns]}


# ─────────────────────────────────────────────────────────────
# 적용
# ─────────────────────────────────────────────────────────────

def _swap_style(ctx: BotContext, new_style: str) -> None:
    """파일 영속화 → settings 재로드 → ctx.settings 변경 가능 필드만 교체.

    trading_lock 으로 진행 중인 사이클/주문과 직렬화. credentials/telegram/kis 등
    런타임에 살아있는 객체는 그대로 두고 prefilter/risk/exit_rules/llm/trade_style
    만 갱신.
    """
    style_switch.write_style(new_style)
    new_settings = load_settings()
    with ctx.trading_lock:
        ctx.settings.prefilter = new_settings.prefilter
        ctx.settings.risk = new_settings.risk
        ctx.settings.exit_rules = new_settings.exit_rules
        ctx.settings.llm = new_settings.llm
        ctx.settings.trade_style = new_settings.trade_style
        ctx.settings.trade_modes = new_settings.trade_modes


def _swap_summary(ctx: BotContext, prev: str, target: str) -> str:
    s = ctx.settings
    return (
        f"✅ *스타일 전환 완료*\n\n"
        f"{style_label(prev)} → {style_label(target)}\n\n"
        f"*새 적용값*\n"
        f"• RSI 매수/매도: < `{s.prefilter.get('rsi_buy_below')}` / > `{s.prefilter.get('rsi_sell_above')}`\n"
        f"• 거래량 최소: `{s.prefilter.get('min_volume_ratio')}x` · "
        f"추세필터: `{'켜짐' if s.prefilter.get('trend_filter_enabled') else '꺼짐'}`\n"
        f"• 확신도: `{s.llm.get('confidence_threshold')}` · "
        f"재거래 대기: `{s.risk.get('cooldown_minutes')}분`\n"
        f"• 일일 주문 한도: `{s.risk.get('max_orders_per_day')}건`\n"
        f"• 손절/익절: `{s.exit_rules.get('stop_loss_pct')}%` / `{s.exit_rules.get('take_profit_pct')}%`\n"
        f"• 트레일링 활성/낙폭: `{s.exit_rules.get('trailing_activation_pct')}%` / "
        f"`{s.exit_rules.get('trailing_distance_pct')}%`\n\n"
        f"_다음 점검부터 새 값 적용. 보유 종목 청산 룰도 새 폭으로 평가됨._"
    )


def handle_style_callback(ctx: BotContext, data: str) -> dict[str, Any]:
    """`style_to:<name>` 콜백 라우팅."""
    if data.startswith("style_to:"):
        target = data.split(":", 1)[1]
        return cmd_style(ctx, [target])
    return _reply(f"❌ 모르는 style 콜백: {data}")
