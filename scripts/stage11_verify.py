"""Stage 11: 거래 스타일 프리셋 검증.

apply_style / read_style / write_style 의 동작을 다음 시나리오로 검증한다:
1. settings.yaml 의 trade_modes 섹션이 prefilter/risk/exit/llm 에 올바르게 오버레이
2. data/trade_mode 영속화 (write/read/clear) 일관성
3. 잘못된 스타일 입력 시 ValueError + default 폴백
4. load_settings() 가 활성 스타일을 반영해 Settings.prefilter 등을 갱신
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(override=True)

from trading_bot.bot import style_switch
from trading_bot.config import ROOT, load_settings


def _check(title: str, ok: bool, detail: str = "") -> bool:
    mark = "✅" if ok else "❌"
    print(f"{mark} {title}")
    if detail:
        print(f"   {detail}")
    return ok


def main() -> int:
    all_ok = True
    raw_text = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    raw_template = yaml.safe_load(raw_text)

    # 1. apply_style — scalp 가 prefilter/risk/exit/llm 오버레이
    raw = copy.deepcopy(raw_template)
    style_switch.apply_style("scalp", raw)
    all_ok &= _check(
        "scalp: prefilter.rsi_buy_below 55 로 오버라이드",
        raw["prefilter"]["rsi_buy_below"] == 55,
        f"실제={raw['prefilter']['rsi_buy_below']}",
    )
    all_ok &= _check(
        "scalp: prefilter.trend_filter_enabled false",
        raw["prefilter"]["trend_filter_enabled"] is False,
    )
    all_ok &= _check(
        "scalp: risk.cooldown_minutes 10",
        raw["risk"]["cooldown_minutes"] == 10,
    )
    all_ok &= _check(
        "scalp: risk.max_orders_per_day 20",
        raw["risk"]["max_orders_per_day"] == 20,
    )
    all_ok &= _check(
        "scalp: exit.stop_loss_pct 2.5",
        raw["exit"]["stop_loss_pct"] == 2.5,
    )
    all_ok &= _check(
        "scalp: llm.confidence_threshold 0.60",
        raw["llm"]["confidence_threshold"] == 0.60,
    )
    # 오버라이드 안 된 키는 기본값 유지
    all_ok &= _check(
        "scalp: 미정의 키 (risk.max_concurrent_positions) 는 기본값 유지",
        raw["risk"]["max_concurrent_positions"]
        == raw_template["risk"]["max_concurrent_positions"],
    )

    # 2. swing 오버레이
    raw = copy.deepcopy(raw_template)
    style_switch.apply_style("swing", raw)
    all_ok &= _check(
        "swing: rsi_buy_below 35 / cooldown 240 / SL 8",
        raw["prefilter"]["rsi_buy_below"] == 35
        and raw["risk"]["cooldown_minutes"] == 240
        and raw["exit"]["stop_loss_pct"] == 8,
    )

    # 3. default 는 무변경
    raw = copy.deepcopy(raw_template)
    before = copy.deepcopy(raw)
    style_switch.apply_style("default", raw)
    all_ok &= _check(
        "default: prefilter/risk/exit/llm 무변경",
        all(raw[k] == before[k] for k in ("prefilter", "risk", "exit", "llm")),
    )

    # 4. 영속화 — write/read/clear
    original_style = style_switch.read_style()
    try:
        style_switch.write_style("scalp")
        all_ok &= _check(
            "write_style('scalp') 후 read_style() == 'scalp'",
            style_switch.read_style() == "scalp",
        )
        all_ok &= _check(
            "data/trade_mode 파일 존재",
            style_switch.STYLE_FILE.exists(),
        )

        # default 쓰면 파일 삭제
        style_switch.write_style("default")
        all_ok &= _check(
            "write_style('default') 는 파일을 제거",
            not style_switch.STYLE_FILE.exists()
            and style_switch.read_style() == "default",
        )

        # 잘못된 입력은 ValueError
        try:
            style_switch.write_style("invalid")
            all_ok &= _check("invalid 스타일 ValueError", False, "raise 안 함")
        except ValueError:
            all_ok &= _check("invalid 스타일 ValueError", True)
    finally:
        # 원복
        if original_style == "default":
            style_switch.clear_style()
        else:
            style_switch.write_style(original_style)

    # 5. load_settings() 통합 — scalp 활성 시 Settings 가 새 값 반영
    try:
        style_switch.write_style("scalp")
        s = load_settings()
        all_ok &= _check(
            "load_settings: trade_style='scalp'",
            s.trade_style == "scalp",
        )
        all_ok &= _check(
            "load_settings: prefilter 오버라이드 반영",
            s.prefilter.get("rsi_buy_below") == 55,
        )
        all_ok &= _check(
            "load_settings: exit_rules 오버라이드 반영",
            s.exit_rules.get("stop_loss_pct") == 2.5,
        )
    finally:
        if original_style == "default":
            style_switch.clear_style()
        else:
            style_switch.write_style(original_style)

    # 6. Stage 12: 수수료 가드 (트레일링) + scalp 트레일링 보수화 확인
    from trading_bot.signals import exit_strategy

    fees_cfg = {
        "commission_per_side_pct": 0.015,
        "sell_tax_pct": 0.20,
        "slippage_per_side_pct": 0.05,
        "min_net_profit_pct": 0.30,
    }
    rt = exit_strategy.round_trip_cost_pct(fees_cfg)
    all_ok &= _check(
        f"round_trip_cost_pct = {rt:.2f}% (≈ 0.33%)",
        abs(rt - 0.33) < 0.01,
    )

    # scalp 트레일링이 가드를 통과 못 하는 시나리오 (gross +0.4%, 수수료 후 +0.07%)
    pos = {"code": "005930", "name": "삼성전자", "cur_price": 100400, "avg_price": 100000, "qty": 10}
    state = {"entry_price": 100000, "high_water_mark": 103000, "trailing_active": True}
    # scalp 룰: activation 3, distance 1, hwm pnl=3% → 활성, drop=-2.52%, 트레일링 트리거
    # 그러나 net pnl=0.07% < min_net=0.30% → 청산 보류
    scalp_exit = (raw_template.get("trade_modes") or {}).get("scalp", {}).get("exit", {})
    scalp_full_exit = {**raw_template["exit"], **scalp_exit}
    d = exit_strategy.check_exit(pos, state, scalp_full_exit, fees=fees_cfg)
    all_ok &= _check(
        "scalp 트레일링 트리거되지만 수수료 가드로 보류",
        d.should_exit is False,
        f"실제 should_exit={d.should_exit} pnl={d.pnl_pct:.2f}%",
    )

    # net pnl 이 가드를 넘으면 정상 청산 (+1% gross → +0.67% net > 0.30%)
    pos = {"code": "005930", "name": "삼성전자", "cur_price": 101000, "avg_price": 100000, "qty": 10}
    state = {"entry_price": 100000, "high_water_mark": 103500, "trailing_active": True}
    d = exit_strategy.check_exit(pos, state, scalp_full_exit, fees=fees_cfg)
    all_ok &= _check(
        "scalp 트레일링: net pnl 충분 → 정상 청산",
        d.should_exit is True and d.tag == "trailing_stop",
        f"실제 should_exit={d.should_exit} tag={d.tag} pnl={d.pnl_pct:.2f}%",
    )

    # 손절은 수수료 가드 영향 없음 — gross -3% 면 net -3.33% 여도 청산
    pos = {"code": "005930", "name": "삼성전자", "cur_price": 97000, "avg_price": 100000, "qty": 10}
    state = {"entry_price": 100000, "high_water_mark": 100000, "trailing_active": False}
    d = exit_strategy.check_exit(pos, state, scalp_full_exit, fees=fees_cfg)
    all_ok &= _check(
        "scalp 손절: 수수료와 무관하게 트리거",
        d.should_exit is True and d.tag == "stop_loss",
    )

    # scalp 트레일링 폭 보수화: activation 3, distance 1
    all_ok &= _check(
        "scalp 트레일링 활성 3 / 낙폭 1 (보수화)",
        scalp_full_exit["trailing_activation_pct"] == 3
        and scalp_full_exit["trailing_distance_pct"] == 1,
    )

    print()
    print("=" * 50)
    if all_ok:
        print("✅ 모든 검증 통과")
        return 0
    print("❌ 일부 검증 실패")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
