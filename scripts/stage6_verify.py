"""Stage 6 exit strategy verification.

exit_strategy.check_exit 를 여러 시나리오로 직접 호출해서
손절/익절/트레일링 스톱 판단이 올바른지 확인.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from trading_bot.config import load_settings
from trading_bot.logging_setup import setup_logging
from trading_bot.signals import exit_strategy


def run_case(title: str, pos: dict, state: dict, cfg: dict, expect_exit: bool, expect_tag: str = ""):
    d = exit_strategy.check_exit(pos, state, cfg)
    ok = d.should_exit == expect_exit and d.tag == expect_tag
    mark = "✅" if ok else "❌"
    print(f"{mark} {title}")
    print(f"   pnl={d.pnl_pct:+.2f}% should_exit={d.should_exit} tag={d.tag!r}")
    if d.reason:
        print(f"   {d.reason}")
    if not ok:
        print(f"   !! 기대값: should_exit={expect_exit} tag={expect_tag!r}")
    return ok


def main() -> int:
    setup_logging(level="WARNING", log_dir=Path("logs"))
    s = load_settings()
    cfg = s.exit_rules
    print(f"exit 설정: {cfg}\n")

    all_ok = True

    # 시나리오 1: 중립 (손익 -2%) — 어느 조건도 미충족
    pos = {"code": "005930", "name": "삼성전자", "cur_price": 196000, "avg_price": 200000, "qty": 10}
    state = {"entry_price": 200000, "high_water_mark": 200000, "trailing_active": False}
    all_ok &= run_case("중립(-2%) — 청산 없음", pos, state, cfg, expect_exit=False)

    # 시나리오 2: 손실 차단 (-5%)
    pos = {"code": "005930", "name": "삼성전자", "cur_price": 190000, "avg_price": 200000, "qty": 10}
    state = {"entry_price": 200000, "high_water_mark": 200000, "trailing_active": False}
    all_ok &= run_case(
        "손실 차단 (-5%) — stop_loss 발동",
        pos, state, cfg, expect_exit=True, expect_tag="stop_loss",
    )

    # 시나리오 3: 손실 차단 경계 (-4.99%, 미발동)
    pos = {"code": "005930", "name": "삼성전자", "cur_price": 190100, "avg_price": 200000, "qty": 10}
    state = {"entry_price": 200000, "high_water_mark": 200000, "trailing_active": False}
    all_ok &= run_case("손실 경계 (-4.95%) — 아직 미발동", pos, state, cfg, expect_exit=False)

    # 시나리오 4: 이익 확정 (+15%)
    pos = {"code": "000660", "name": "SK하이닉스", "cur_price": 115000, "avg_price": 100000, "qty": 5}
    state = {"entry_price": 100000, "high_water_mark": 115000, "trailing_active": True}
    all_ok &= run_case(
        "이익 확정 (+15%) — take_profit 발동",
        pos, state, cfg, expect_exit=True, expect_tag="take_profit",
    )

    # 시나리오 5: 트레일링 — 활성 조건 미충족 (최고점 +5%, 현재 +3%)
    pos = {"code": "035720", "name": "카카오", "cur_price": 51500, "avg_price": 50000, "qty": 100}
    state = {"entry_price": 50000, "high_water_mark": 52500, "trailing_active": False}
    all_ok &= run_case(
        "트레일링 미활성 (hwm +5% < activation +7%) — 미발동",
        pos, state, cfg, expect_exit=False,
    )

    # 시나리오 6: 트레일링 — 활성, 낙폭 -4.5% (발동)
    # entry 50000, hwm 54000 (+8%), cur 51570 (+3.14%, hwm 대비 -4.5%)
    pos = {"code": "035420", "name": "NAVER", "cur_price": 51570, "avg_price": 50000, "qty": 100}
    state = {"entry_price": 50000, "high_water_mark": 54000, "trailing_active": True}
    all_ok &= run_case(
        "트레일링 활성 + 낙폭 -4.5% — trailing_stop 발동",
        pos, state, cfg, expect_exit=True, expect_tag="trailing_stop",
    )

    # 시나리오 7: 트레일링 — 활성, 낙폭 -3% (아직 미발동, 기준 -4%)
    # entry 50000, hwm 54000, cur 52380 (hwm 대비 -3%)
    pos = {"code": "005380", "name": "현대차", "cur_price": 52380, "avg_price": 50000, "qty": 100}
    state = {"entry_price": 50000, "high_water_mark": 54000, "trailing_active": True}
    all_ok &= run_case(
        "트레일링 활성 + 낙폭 -3% (기준 미달) — 미발동",
        pos, state, cfg, expect_exit=False,
    )

    # 시나리오 8: 손절과 이익 동시 — 손절 우선 (우선순위 검증)
    # 이건 논리적으로 말이 안 되지만 엣지 케이스 테스트: entry=100, cur=80 → -20%
    # take_profit 조건 불가능하지만, stop_loss가 먼저 잡혀야 함
    pos = {"code": "X", "name": "X", "cur_price": 80, "avg_price": 100, "qty": 1}
    state = {"entry_price": 100, "high_water_mark": 120, "trailing_active": True}
    all_ok &= run_case(
        "극단 손실 (-20%) — stop_loss 우선",
        pos, state, cfg, expect_exit=True, expect_tag="stop_loss",
    )

    # update_high_water_mark 테스트
    print("\n--- update_high_water_mark ---")
    # 가상 DB에 접근하는 함수라 실제로는 repo를 거치는데, 여기선 로직만 체크
    # sync_position_state 도 repo 필요해서 real DB로만 가능
    # → 실제 포지션 없이는 통합 테스트 어려움. 로직은 check_exit 로 검증 완료.

    print(f"\n{'✅ 모두 통과' if all_ok else '❌ 일부 실패'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
