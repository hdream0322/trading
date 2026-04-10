from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trading_bot.config import Settings
from trading_bot.risk import kill_switch
from trading_bot.store import repo

log = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    qty: int = 0


class RiskManager:
    """주문 실행 직전의 게이트. 모든 매수/매도 결정은 여기를 통과해야 한다."""

    def __init__(self, settings: Settings):
        risk = settings.risk or {}
        self.max_pos_pct = float(risk.get("max_position_per_symbol_pct", 20)) / 100.0
        self.max_concurrent = int(risk.get("max_concurrent_positions", 5))
        self.daily_loss_limit_pct = float(risk.get("daily_loss_limit_pct", 3))
        self.cooldown_minutes = int(risk.get("cooldown_minutes", 30))
        self.max_orders_per_day = int(risk.get("max_orders_per_day", 10))

    def check(
        self,
        side: str,
        code: str,
        name: str,
        current_price: float,
        balance_summary: dict[str, Any],
        holdings: dict[str, dict[str, Any]],
    ) -> RiskDecision:
        """단일 시그널에 대한 게이트 검사. 통과 시 주문 수량 포함 반환."""
        if side not in {"buy", "sell"}:
            return RiskDecision(False, f"알 수 없는 side: {side}")
        if current_price <= 0:
            return RiskDecision(False, "현재가 0 또는 음수")

        # 1. 킬스위치 (매수만 차단, 매도는 손절/청산 허용)
        if side == "buy" and kill_switch.is_active():
            return RiskDecision(False, "KILL SWITCH 활성")

        # 2. 일일 주문 수 한도
        today_orders = repo.get_today_order_count()
        if today_orders >= self.max_orders_per_day:
            return RiskDecision(False, f"일일 주문 한도 초과 ({today_orders}/{self.max_orders_per_day})")

        # 3. 일일 손실 한도 (KIS 잔고의 asst_icdc_erng_rt 사용 — 전일 대비 자산 증감율)
        if side == "buy":  # 손절 매도는 손실 중에도 허용
            try:
                daily_pct = float(balance_summary.get("asst_icdc_erng_rt") or 0)
            except (ValueError, TypeError):
                daily_pct = 0.0
            if daily_pct < -self.daily_loss_limit_pct:
                return RiskDecision(
                    False,
                    f"일일 손실 한도 초과 ({daily_pct:.2f}% < -{self.daily_loss_limit_pct:.2f}%)",
                )

        # 4. 쿨다운: 같은 종목 최근 주문
        last_ts = repo.get_last_order_ts(code)
        if last_ts:
            try:
                elapsed_min = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() / 60
                if elapsed_min < self.cooldown_minutes:
                    return RiskDecision(
                        False,
                        f"쿨다운 중 ({elapsed_min:.0f}/{self.cooldown_minutes}분)",
                    )
            except ValueError:
                pass

        # 5. 매도 경로: 실제 보유 중인지 확인
        if side == "sell":
            pos = holdings.get(code)
            if not pos or pos["qty"] <= 0:
                return RiskDecision(False, "미보유 종목 — 매도 불가")
            return RiskDecision(True, "sell allowed (전량 시장가)", qty=int(pos["qty"]))

        # 6. 매수 경로
        # 6a. 이미 보유 중이면 추매 금지 (stage 4+에서 피라미딩 고려)
        if code in holdings and holdings[code]["qty"] > 0:
            return RiskDecision(False, "이미 보유 중 — 추매 금지")

        # 6b. 동시 보유 종목 수 한도
        current_positions = sum(1 for p in holdings.values() if p["qty"] > 0)
        if current_positions >= self.max_concurrent:
            return RiskDecision(
                False,
                f"동시 보유 한도 초과 ({current_positions}/{self.max_concurrent})",
            )

        # 6c. 포지션 사이징
        tot_eval = float(balance_summary.get("tot_evlu_amt") or 0)  # 총평가금액
        dnca = float(balance_summary.get("dnca_tot_amt") or 0)       # 예수금
        if tot_eval <= 0 or dnca <= 0:
            return RiskDecision(False, f"잔고 비어있음 (eval={tot_eval} dnca={dnca})")

        budget = tot_eval * self.max_pos_pct
        # 예수금보다 크면 예수금의 95% 이내로 제한 (수수료/슬리피지 마진)
        if budget > dnca:
            budget = dnca * 0.95

        qty = int(budget // current_price)
        if qty < 1:
            return RiskDecision(
                False,
                f"예산 부족 (budget={budget:.0f}, price={current_price:.0f})",
            )

        return RiskDecision(
            True,
            f"buy allowed (budget={budget:.0f} / {qty}주)",
            qty=qty,
        )
