from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Candidate:
    code: str
    name: str
    side_hint: str  # "buy" | "sell"
    features: dict[str, Any]


def evaluate(features: dict[str, Any], config: dict[str, Any]) -> Candidate | None:
    """룰베이스 사전필터. LLM에 넘길 후보만 반환.

    통과 조건:
      - RSI 과매도(< rsi_buy_below) + 거래량 비율 >= min_volume_ratio → buy 후보
      - RSI 과매수(> rsi_sell_above) + 거래량 비율 >= min_volume_ratio → sell 후보
    """
    rsi_val = float(features["rsi"])
    vol_ratio = float(features["volume_ratio"])

    rsi_buy = float(config.get("rsi_buy_below", 35))
    rsi_sell = float(config.get("rsi_sell_above", 70))
    min_vol = float(config.get("min_volume_ratio", 1.2))

    side: str | None = None
    if rsi_val < rsi_buy and vol_ratio >= min_vol:
        side = "buy"
    elif rsi_val > rsi_sell and vol_ratio >= min_vol:
        side = "sell"

    if side is None:
        return None

    return Candidate(
        code=features["code"],
        name=features["name"],
        side_hint=side,
        features=features,
    )
