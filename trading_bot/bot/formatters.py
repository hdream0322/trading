"""사용자 대면 텍스트 포맷 헬퍼 — 토스 증권처럼 읽기 쉽게."""
from __future__ import annotations

from typing import Any


def fmt_won(value: Any, fallback: str = "?") -> str:
    """10000000 → '10,000,000원'"""
    try:
        return f"{int(float(value)):,}원"
    except (ValueError, TypeError):
        return fallback


def fmt_pct(value: Any, fallback: str = "?") -> str:
    """부호 붙여서 퍼센트 표시. 1.23 → '+1.23%'"""
    try:
        return f"{float(value):+.2f}%"
    except (ValueError, TypeError):
        return fallback


def decision_ko(decision: str) -> str:
    """buy/sell/hold → 구매/판매/관망"""
    return {"buy": "구매", "sell": "판매", "hold": "관망"}.get(decision, decision)


def mode_badge(mode: str) -> str:
    return "🔴 실전" if mode == "live" else "🟡 모의"


def confidence_pct(value: float | None) -> str:
    """0.75 → '75%'"""
    if value is None:
        return ""
    return f"{int(round(value * 100))}%"


def fmt_uptime(delta_seconds: float) -> str:
    """초 단위 업타임을 '3일 2시간 15분' 형태로."""
    total = int(delta_seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    parts.append(f"{minutes}분")
    return " ".join(parts)
