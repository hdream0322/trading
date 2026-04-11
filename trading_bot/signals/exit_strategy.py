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
    pnl_pct: float    # 현재 손익률
    entry_price: float
    current_price: float


def sync_position_state(
    holdings: dict[str, dict[str, Any]],
    now_iso: str,
) -> dict[str, dict[str, Any]]:
    """KIS 잔고와 position_state 테이블을 동기화.

    - 신규 포지션: state 생성 (entry_price = KIS avg_price, hwm = 현재가)
    - 사라진 포지션: state 삭제
    - 유지 중인 포지션: high_water_mark 갱신

    반환: 동기화 후의 state dict {code: state_dict}
    """
    existing = repo.get_all_position_states()

    # 신규 진입 포지션 기록
    for code, pos in holdings.items():
        if code in existing:
            continue
        repo.insert_position_state(
            code=code,
            name=str(pos.get("name", "")),
            entry_ts=now_iso,
            entry_price=float(pos["avg_price"]),
            high_water_mark=float(pos["cur_price"]),
            trailing_active=False,
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
) -> ExitDecision:
    """단일 포지션에 대한 청산 여부 판단.

    세 가지 기계적 규칙:
    1. 손실 차단 (stop loss): 손익률 <= -stop_loss_pct
    2. 이익 확정 (take profit): 손익률 >= take_profit_pct
    3. 고점 대비 하락 (trailing stop): 최고점 대비 trailing_distance_pct 하락

    트레일링 스톱은 손익률이 trailing_activation_pct 를 한 번이라도 넘은 이후에만 동작.

    dynamic_stop_loss_pct 가 주어지면 고정 stop_loss_pct 대신 사용 (ATR 기반 동적 손절).
    """
    cur = float(pos["cur_price"])
    entry = float(state.get("entry_price") or pos["avg_price"])
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

    # 1. 손실 차단
    if pnl_pct <= -stop_loss_pct:
        return ExitDecision(
            True,
            "stop_loss",
            f"🛡️ 손실 차단: {name} {pnl_pct:+.2f}% (기준 -{stop_loss_pct:.1f}%)",
            pnl_pct, entry, cur,
        )

    # 2. 이익 확정 (고정)
    if pnl_pct >= take_profit_pct:
        return ExitDecision(
            True,
            "take_profit",
            f"🎯 이익 확정: {name} {pnl_pct:+.2f}% (기준 +{take_profit_pct:.1f}%)",
            pnl_pct, entry, cur,
        )

    # 3. 고점 대비 하락 (트레일링 스톱)
    # 활성 조건: hwm 기준 손익률이 활성화 임계값 이상이었어야 함
    hwm_pnl_pct = (hwm - entry) / entry * 100
    if hwm_pnl_pct >= trailing_activation_pct:
        # 최고점 대비 현재가 낙폭
        drop_from_hwm = (cur - hwm) / hwm * 100
        if drop_from_hwm <= -trailing_distance_pct:
            return ExitDecision(
                True,
                "trailing_stop",
                (
                    f"📉 고점 대비 하락: {name} "
                    f"최고 +{hwm_pnl_pct:.1f}% → 현재 {pnl_pct:+.2f}% "
                    f"(고점 대비 {drop_from_hwm:.2f}%, 기준 -{trailing_distance_pct:.1f}%)"
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

    new_hwm = max(old_hwm, current_price)
    hwm_pnl_pct = (new_hwm - entry) / entry * 100 if entry > 0 else 0.0

    trailing_activation_pct = float(config.get("trailing_activation_pct", 7))
    new_trailing = old_trailing or (hwm_pnl_pct >= trailing_activation_pct)

    if new_hwm != old_hwm or new_trailing != old_trailing:
        repo.update_position_hwm(code, new_hwm, new_trailing)

    return new_hwm, new_trailing
