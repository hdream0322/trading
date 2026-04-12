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
        self.max_per_sector = int(risk.get("max_per_sector", 2))
        self.daily_loss_limit_pct = float(risk.get("daily_loss_limit_pct", 3))
        self.cooldown_minutes = int(risk.get("cooldown_minutes", 30))
        self.max_orders_per_day = int(risk.get("max_orders_per_day", 10))
        # Stage 10: 펀더멘털 게이트
        funda = getattr(settings, "fundamentals", None) or {}
        # 런타임 오버라이드(data/FUNDA_ENABLED) 를 우선, 없으면 settings.yaml 값
        from trading_bot.bot.commands_funda import is_enabled as _funda_is_enabled
        self.funda_enabled = _funda_is_enabled(funda)
        self.funda_max_per = float(funda.get("max_per", 50))
        self.funda_min_per = float(funda.get("min_per", 0))
        self.funda_max_pbr = float(funda.get("max_pbr", 10))
        self.funda_max_debt_ratio = float(funda.get("max_debt_ratio", 300))
        self.funda_min_roe = float(funda.get("min_roe", -5))

    def check(
        self,
        side: str,
        code: str,
        name: str,
        current_price: float,
        balance_summary: dict[str, Any],
        holdings: dict[str, dict[str, Any]],
        is_exit: bool = False,
        candidate_sector: str | None = None,
        holdings_by_sector: dict[str, int] | None = None,
        fundamentals: dict[str, Any] | None = None,
    ) -> RiskDecision:
        """단일 시그널에 대한 게이트 검사. 통과 시 주문 수량 포함 반환.

        is_exit=True: 손절/익절/트레일링 스톱 등 기계적 청산 판매.
          일일 주문 수 한도를 우회 (포지션 보호가 한도보다 우선).
        """
        if side not in {"buy", "sell"}:
            return RiskDecision(False, f"알 수 없는 side: {side}")
        if current_price <= 0:
            return RiskDecision(False, "현재가 0 또는 음수")

        # 1. 긴급 정지 (구매만 차단, 판매는 손절/청산 허용)
        if side == "buy" and kill_switch.is_active():
            return RiskDecision(False, "긴급 정지 켜짐")

        # 2. 일일 주문 수 한도 — 청산 판매는 우회
        if not is_exit:
            today_orders = repo.get_today_order_count()
            if today_orders >= self.max_orders_per_day:
                return RiskDecision(
                    False,
                    f"오늘 주문 횟수 제한 도달 ({today_orders}/{self.max_orders_per_day})",
                )

        # 3. 일일 손실 한도 (KIS 잔고의 asst_icdc_erng_rt 사용 — 전일 대비 자산 증감율)
        if side == "buy":  # 손절 판매는 손실 중에도 허용
            try:
                daily_pct = float(balance_summary.get("asst_icdc_erng_rt") or 0)
            except (ValueError, TypeError):
                daily_pct = 0.0
            if daily_pct < -self.daily_loss_limit_pct:
                return RiskDecision(
                    False,
                    f"오늘 손실 너무 큼 ({daily_pct:.2f}% · 한도 -{self.daily_loss_limit_pct:.2f}%)",
                )

        # 4. 쿨다운: 같은 종목 최근 주문
        last_ts = repo.get_last_order_ts(code)
        if last_ts:
            try:
                elapsed_min = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() / 60
                if elapsed_min < self.cooldown_minutes:
                    return RiskDecision(
                        False,
                        f"재거래 대기 중 ({elapsed_min:.0f}/{self.cooldown_minutes}분)",
                    )
            except ValueError:
                pass

        # 5. 판매 경로: 실제 보유 중인지 확인
        if side == "sell":
            pos = holdings.get(code)
            if not pos or pos["qty"] <= 0:
                return RiskDecision(False, "갖고 있지 않음 — 판매 불가")
            return RiskDecision(True, "판매 가능 (전량)", qty=int(pos["qty"]))

        # 6. 구매 경로
        # 6a. 이미 보유 중이면 추가 구매 금지
        if code in holdings and holdings[code]["qty"] > 0:
            return RiskDecision(False, "이미 갖고 있음 — 추가 구매 금지")

        # 6b. 동시 보유 종목 수 한도
        current_positions = sum(1 for p in holdings.values() if p["qty"] > 0)
        if current_positions >= self.max_concurrent:
            return RiskDecision(
                False,
                f"동시에 갖고 있는 주식 수 제한 ({current_positions}/{self.max_concurrent})",
            )

        # 6b-2. 섹터(업종) 분산 한도 — 같은 업종이 max_per_sector 개 이상이면 차단.
        # candidate_sector 또는 holdings_by_sector 가 없으면 게이트 우회 (sector 미분류).
        if candidate_sector and holdings_by_sector is not None:
            held_in_sector = int(holdings_by_sector.get(candidate_sector, 0))
            if held_in_sector >= self.max_per_sector:
                return RiskDecision(
                    False,
                    f"같은 업종({candidate_sector}) 보유 한도 "
                    f"({held_in_sector}/{self.max_per_sector})",
                )

        # 6b-3. 펀더멘털 게이트 — 재무지표 비정상 종목 매수 차단
        # 매도는 허용 (이미 보유한 부실 종목은 빠져나와야 함)
        # 데이터 없음(캐시 미스) → 차단하지 않음 (연동 장애 방지)
        if self.funda_enabled and fundamentals:
            funda_reason = self._check_fundamentals(fundamentals)
            if funda_reason:
                return RiskDecision(False, funda_reason)

        # 6c. 포지션 사이징
        tot_eval = float(balance_summary.get("tot_evlu_amt") or 0)   # 총 자산
        dnca = float(balance_summary.get("dnca_tot_amt") or 0)        # 쓸 수 있는 현금
        if tot_eval <= 0 or dnca <= 0:
            return RiskDecision(False, f"계좌 비어있음 (평가={tot_eval} 현금={dnca})")

        budget = tot_eval * self.max_pos_pct
        # 현금보다 크면 현금의 95% 이내로 제한 (수수료/슬리피지 마진)
        if budget > dnca:
            budget = dnca * 0.95

        qty = int(budget // current_price)
        if qty < 1:
            return RiskDecision(
                False,
                f"예산 부족 (예산 {int(budget):,}원 / 가격 {int(current_price):,}원)",
            )

        return RiskDecision(
            True,
            f"구매 가능 (예산 {int(budget):,}원 → {qty}주)",
            qty=qty,
        )

    def _check_fundamentals(self, f: dict[str, Any]) -> str | None:
        """재무지표 임계값 검사. 위반 시 차단 사유 문자열, 통과 시 None.

        개별 지표가 None 이면 해당 검사를 스킵한다 (데이터 없음 ≠ 차단).
        """
        per = f.get("per")
        if per is not None:
            if per < self.funda_min_per:
                return f"PER 비정상 ({per:.1f} < 최소 {self.funda_min_per:.0f}) — 적자 기업"
            if per > self.funda_max_per:
                return f"PER 과도 ({per:.1f} > 최대 {self.funda_max_per:.0f})"

        pbr = f.get("pbr")
        if pbr is not None and pbr > self.funda_max_pbr:
            return f"PBR 과도 ({pbr:.1f} > 최대 {self.funda_max_pbr:.0f})"

        debt = f.get("debt_ratio")
        if debt is not None and debt > self.funda_max_debt_ratio:
            return f"부채비율 과도 ({debt:.0f}% > 최대 {self.funda_max_debt_ratio:.0f}%)"

        roe = f.get("roe")
        if roe is not None and roe < self.funda_min_roe:
            return f"ROE 부진 ({roe:.1f}% < 최소 {self.funda_min_roe:.0f}%)"

        return None
