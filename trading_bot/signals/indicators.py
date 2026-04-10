from __future__ import annotations

from typing import Sequence


def rsi(closes: Sequence[float], period: int = 14) -> float:
    """Wilder's RSI. 마지막 시점의 값을 반환."""
    if len(closes) < period + 1:
        raise ValueError(f"RSI 계산에 최소 {period + 1}개 종가 필요 (현재 {len(closes)})")

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volume_ratio(volumes: Sequence[float], lookback: int = 20) -> float:
    """최근 거래량 대비 과거 lookback일 평균 거래량 비율."""
    if len(volumes) < lookback + 1:
        return 1.0
    latest = volumes[-1]
    past = volumes[-lookback - 1:-1]
    avg = sum(past) / len(past)
    if avg == 0:
        return 1.0
    return latest / avg


def sma(values: Sequence[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"SMA({period}) 계산에 최소 {period}개 필요")
    return sum(values[-period:]) / period
