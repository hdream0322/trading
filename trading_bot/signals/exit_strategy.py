from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trading_bot.store import repo

log = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """단일 포지션에 대한 청산 판단 결과."""
    should_exit: bool
    tag: str          # "stop_loss" | "take_profit" | "trailing_stop" | ""
    reason: str       # 사용자에게 보여줄 설명 (쉬운 한국어)
    pnl_pct: float    # 현재 손익률 (gross %)
    entry_price: float
    current_price: float


def round_trip_cost_pct(fees: dict[str, Any] | None) -> float:
    """매수 + 매도 왕복 비용 (%, 수수료 + 거래세 + 슬리피지).

    fees=None 또는 키 없음이면 0 — 폴백 동작은 기존 (수수료 미반영) 그대로.
    """
    if not fees:
        return 0.0
    commission = float(fees.get("commission_per_side_pct", 0)) * 2
    sell_tax = float(fees.get("sell_tax_pct", 0))
    slippage = float(fees.get("slippage_per_side_pct", 0)) * 2
    return commission + sell_tax + slippage


def net_pnl_pct(gross_pnl_pct: float, fees: dict[str, Any] | None) -> float:
    """gross 손익률에서 왕복 비용 차감한 net 손익률 (%)."""
    return gross_pnl_pct - round_trip_cost_pct(fees)


def sync_position_state(
    holdings: dict[str, dict[str, Any]],
    now_iso: str,
) -> dict[str, dict[str, Any]]:
    """KIS 잔고와 position_state 테이블을 동기화.

    - 신규 포지션: state 생성 (entry_price = KIS avg_price, hwm = 현재가)
    - 사라진 포지션: state 삭제
    - 유지 중인 포지션: high_water_mark 갱신, 추가매수 감지 시 cost_basis 갱신

    entry_price 보존 정책:
    - 기존 row 가 있으면 entry_price 는 절대 KIS avg 로 덮어쓰지 않음.
    - 추가매수로 qty 증가 + KIS avg 변경이 감지되면 cost_basis 만 갱신.
    - hwm 은 기존 값 유지 (보수 정책, 손절이 늦어지지 않게).
    - entry_price 는 처음 진입 가격 (사용자가 인식하는 기준가).
    - cost_basis 는 추가매수 후 KIS 가중평균 (trailing 활성화 임계 비교용).

    반환: 동기화 후의 state dict {code: state_dict}
    """
    existing = repo.get_all_position_states()

    # 신규 진입 포지션 기록
    for code, pos in holdings.items():
        if code in existing:
            # 기존 포지션: KIS avg 가 기존 cost_basis 와 달라졌으면 추가매수로 판단.
            # entry_price 와 hwm 은 보존 (보수 정책).
            kis_avg = float(pos["avg_price"])
            prev_cost = float(existing[code].get("cost_basis") or existing[code]["entry_price"])
            if abs(kis_avg - prev_cost) > 0.5:
                repo.update_position_cost_basis(code, kis_avg)
                log.info(
                    "평단 변경 감지 — cost_basis 갱신: %s %.0f→%.0f (entry_price·hwm 보존)",
                    code, prev_cost, kis_avg,
                )
            continue
        repo.insert_position_state(
            code=code,
            name=str(pos.get("name", "")),
            entry_ts=now_iso,
            entry_price=float(pos["avg_price"]),
            high_water_mark=float(pos["cur_price"]),
            trailing_active=False,
            cost_basis=float(pos["avg_price"]),
        )
        log.info(
            "신규 포지션 state 기록: %s %s entry=%.0f",
            code, pos.get("name", ""), pos["avg_price"],
        )

    # 사라진 포지션 정리
    for code in list(existing.keys()):
        if code not in holdings:
            repo.delete_position_state(code)
            log.info("청산된 포지션 state 삭제: %s", code)

    # 최신 state 재조회
    return repo.get_all_position_states()


def check_exit(
    pos: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    dynamic_stop_loss_pct: float | None = None,
    fees: dict[str, Any] | None = None,
) -> ExitDecision:
    """단일 포지션에 대한 청산 여부 판단.

    세 가지 기계적 규칙:
    1. 손실 차단 (stop loss): 손익률 <= -stop_loss_pct
    2. 이익 확정 (take profit): 손익률 >= take_profit_pct
    3. 고점 대비 하락 (trailing stop): 최고점 대비 trailing_distance_pct 하락 +
       net pnl (수수료 차감) >= fees.min_net_profit_pct 가드

    트레일링 스톱은 손익률이 trailing_activation_pct 를 한 번이라도 넘은 이후에만 동작.
    수수료 못 버는 트레일링 청산을 막기 위해 net pnl 가드를 추가 — 스톱로스/익절은
    영향 없음 (손절은 어차피 손실 인정, 고정 익절은 임계값을 사용자가 정함).

    dynamic_stop_loss_pct 가 주어지면 고정 stop_loss_pct 대신 사용 (ATR 기반 동적 손절).
    fees=None 이면 수수료 미반영 (기존 동작).
    """
    cur = float(pos["cur_price"])
    entry = float(state.get("entry_price") or pos["avg_price"])
    # cost_basis: 추가매수 후 KIS 가중평균. 없으면 entry_price 로 폴백.
    # trailing 활성화 임계 비교에 사용 (추가매수 후 평단 기준으로 판단).
    cost_basis = float(state.get("cost_basis") or entry)
    name = pos.get("name", pos.get("code", "?"))

    if entry <= 0 or cur <= 0:
        return ExitDecision(False, "", "가격 정보 없음", 0.0, entry, cur)

    pnl_pct = (cur - entry) / entry * 100
    hwm = float(state.get("high_water_mark") or cur)

    if dynamic_stop_loss_pct is not None:
        stop_loss_pct = float(dynamic_stop_loss_pct)
    else:
        stop_loss_pct = float(config.get("stop_loss_pct", 5))
    take_profit_pct = float(config.get("take_profit_pct", 15))
    trailing_activation_pct = float(config.get("trailing_activation_pct", 7))
    trailing_distance_pct = float(config.get("trailing_distance_pct", 4))

    rt_cost = round_trip_cost_pct(fees)
    net_pct = pnl_pct - rt_cost
    net_suffix = f" · 수수료 후 {net_pct:+.2f}%" if rt_cost > 0 else ""

    # 1. 손실 차단
    if pnl_pct <= -stop_loss_pct:
        return ExitDecision(
            True,
            "stop_loss",
            f"🛡️ 손실 차단: {name} {pnl_pct:+.2f}% (기준 -{stop_loss_pct:.1f}%){net_suffix}",
            pnl_pct, entry, cur,
        )

    # 2. 이익 확정 (고정)
    if pnl_pct >= take_profit_pct:
        return ExitDecision(
            True,
            "take_profit",
            f"🎯 이익 확정: {name} {pnl_pct:+.2f}% (기준 +{take_profit_pct:.1f}%){net_suffix}",
            pnl_pct, entry, cur,
        )

    # 3. 고점 대비 하락 (트레일링 스톱)
    # 활성 조건: hwm 이 cost_basis 대비 활성화 임계값 이상이었어야 함.
    # cost_basis = 추가매수 후 KIS 가중평균 (없으면 entry_price).
    # hwm 은 기존 값 보존 (보수 정책).
    hwm_pnl_pct = (hwm - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0.0
    if hwm_pnl_pct >= trailing_activation_pct:
        # 최고점 대비 현재가 낙폭
        drop_from_hwm = (cur - hwm) / hwm * 100
        if drop_from_hwm <= -trailing_distance_pct:
            # 수수료 가드 — net pnl 이 min_net_profit_pct 미만이면 청산 보류 (재상승 대기).
            # rt_cost==0 (fees 미설정) 이면 가드 미동작 → 기존 동작.
            min_net = float((fees or {}).get("min_net_profit_pct", 0.0))
            if rt_cost > 0 and net_pct < min_net:
                log.info(
                    "트레일링 청산 보류 (수수료 못 김): %s gross=%+.2f%% net=%+.2f%% "
                    "min_net=%.2f%%",
                    name, pnl_pct, net_pct, min_net,
                )
                return ExitDecision(False, "", "", pnl_pct, entry, cur)
            return ExitDecision(
                True,
                "trailing_stop",
                (
                    f"📉 고점 대비 하락: {name} "
                    f"최고 +{hwm_pnl_pct:.1f}% → 현재 {pnl_pct:+.2f}% "
                    f"(고점 대비 {drop_from_hwm:.2f}%, 기준 -{trailing_distance_pct:.1f}%)"
                    f"{net_suffix}"
                ),
                pnl_pct, entry, cur,
            )

    return ExitDecision(False, "", "", pnl_pct, entry, cur)


def update_high_water_mark(
    code: str,
    state: dict[str, Any],
    current_price: float,
    config: dict[str, Any],
) -> tuple[float, bool]:
    """high_water_mark 갱신. 필요 시 trailing_active 도 True 로 전환.

    반환: (새 hwm, 새 trailing_active)
    """
    old_hwm = float(state.get("high_water_mark") or current_price)
    old_trailing = bool(state.get("trailing_active"))
    entry = float(state.get("entry_price") or current_price)
    # trailing 활성화 임계는 cost_basis 기준 (추가매수 후 평단 반영).
    cost_basis = float(state.get("cost_basis") or entry)

    new_hwm = max(old_hwm, current_price)
    hwm_pnl_pct = (new_hwm - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0.0

    trailing_activation_pct = float(config.get("trailing_activation_pct", 7))
    new_trailing = old_trailing or (hwm_pnl_pct >= trailing_activation_pct)

    if new_hwm != old_hwm or new_trailing != old_trailing:
        repo.update_position_hwm(code, new_hwm, new_trailing)

    return new_hwm, new_trailing
