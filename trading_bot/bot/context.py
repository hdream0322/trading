from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.config import Settings
    from trading_bot.kis.client import KisClient
    from trading_bot.risk.manager import RiskManager
    from trading_bot.signals.llm import ClaudeSignalClient


@dataclass
class BotContext:
    """커맨드 핸들러와 poller가 공유하는 상태.

    trading_lock은 "자동 사이클의 주문 실행"과 "수동 /sell"을 직렬화해서
    동시에 같은 종목 또는 잔고에 대해 경합이 나지 않도록 한다.
    """
    settings: "Settings"
    kis: "KisClient"
    risk: "RiskManager"
    llm: "ClaudeSignalClient | None"
    trading_lock: Lock = field(default_factory=Lock)
