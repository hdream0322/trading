"""청산 사유 라벨 — repo.get_last_stop_loss_ts 의 LIKE 매칭과 cycle.py 의
reason 포맷이 한 곳에서만 정의되도록.
"""
from __future__ import annotations

EXIT_REASON_PREFIX = "exit"
EXIT_TAG_STOP_LOSS = "stop_loss"
EXIT_TAG_TAKE_PROFIT = "take_profit"
EXIT_TAG_TRAILING = "trailing"

def format_exit_reason(tag: str, detail: str) -> str:
    return f"{EXIT_REASON_PREFIX} ({tag}): {detail}"

def stop_loss_reason_like_pattern() -> str:
    return f"{EXIT_REASON_PREFIX} ({EXIT_TAG_STOP_LOSS}):%"
