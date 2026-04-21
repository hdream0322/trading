from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Candidate:
    code: str
    name: str
    side_hint: str  # "buy" | "sell"
    features: dict[str, Any]


def evaluate(
    features: dict[str, Any],
    config: dict[str, Any],
    held: bool = False,
) -> Candidate | None:
    """룰베이스 사전필터. LLM에 넘길 후보만 반환.

    통과 조건 (buy):
      - RSI 과매도(<= rsi_buy_below) + 거래량 비율 >= min_volume_ratio
      - 추세 필터가 켜져 있으면: 현재가 > SMA(trend_sma_period) (하락 추세 중 칼날 차단)
    통과 조건 (sell):
      - 보유 중인 종목 (held=True) 일 때만 후보 자격
      - RSI 과매수(>= rsi_sell_above) + 거래량 비율 >= min_volume_ratio
    """
    rsi_val = float(features["rsi"])
    vol_ratio = float(features["volume_ratio"])

    rsi_buy = float(config.get("rsi_buy_below", 35))
    rsi_sell = float(config.get("rsi_sell_above", 70))
    min_vol = float(config.get("min_volume_ratio", 1.2))
    trend_filter_on = bool(config.get("trend_filter_enabled", True))

    side: str | None = None
    if rsi_val <= rsi_buy and vol_ratio >= min_vol:
        # 추세 필터: 이동평균선 아래면 "떨어지는 칼날" 로 간주, 후보 제외
        if trend_filter_on:
            cur = float(features.get("current_price") or 0)
            sma_val = features.get("sma_trend")
            if sma_val is None or cur <= 0:
                return None  # 데이터 부족 시 보수적으로 거름
            if cur <= float(sma_val):
                return None
        side = "buy"
    elif rsi_val >= rsi_sell and vol_ratio >= min_vol:
        # 미보유 종목의 sell 후보는 risk 게이트에서 어차피 차단됨.
        # 여기서 미리 걸러 LLM 비용 낭비를 막는다.
        if not held:
            return None
        side = "sell"

    if side is None:
        return None

    return Candidate(
        code=features["code"],
        name=features["name"],
        side_hint=side,
        features=features,
    )
