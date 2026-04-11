from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from trading_bot.notify import telegram

log = logging.getLogger(__name__)

# 누적 LLM 비용이 임계를 처음 넘는 사이클에서 텔레그램으로 1회만 경고/알람.
# 중복 방지는 data/ 안의 날짜별 마커 파일로. 다음 날 0시 지나면 파일명이 바뀌어 자동 리셋.
# 마커가 만들어지는 건 텔레그램 발송이 성공한 뒤 — 발송 실패 시 다음 사이클에 재시도된다.
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def _marker_path(name: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return _DATA_DIR / f"{name}_{today}"


def _fire_once(marker_name: str, telegram_cfg, message: str) -> None:
    marker = _marker_path(marker_name)
    if marker.exists():
        return
    try:
        telegram.send(telegram_cfg, message)
    except Exception:
        log.exception("LLM 비용 알람 전송 실패 (%s)", marker_name)
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception:
        log.exception("LLM 비용 알람 마커 생성 실패 (%s)", marker_name)


def maybe_warn(
    daily_cost: float,
    warn_threshold: float,
    daily_limit: float,
    telegram_cfg,
) -> None:
    """누적 비용이 경고선을 처음 넘는 순간 1회 텔레그램 알림."""
    if warn_threshold <= 0 or daily_cost < warn_threshold:
        return
    msg = (
        f"⚠️ *AI 비용 경고선 돌파*\n"
        f"오늘 누적 *${daily_cost:.4f}* / 경고선 ${warn_threshold:.2f} / "
        f"한도 ${daily_limit:.2f}\n"
        f"평소(하루 $0.10 안쪽) 보다 호출이 많아요. `/cost` 로 확인해 보세요."
    )
    _fire_once("llm_cost_warned", telegram_cfg, msg)


def maybe_alert_limit(
    daily_cost: float,
    daily_limit: float,
    telegram_cfg,
) -> None:
    """누적 비용이 하드 한도에 도달해 LLM 호출이 차단되는 순간 1회 텔레그램 알림."""
    if daily_limit <= 0 or daily_cost < daily_limit:
        return
    msg = (
        f"🛑 *AI 비용 한도 도달 — LLM 호출 차단*\n"
        f"오늘 누적 *${daily_cost:.4f}* / 한도 ${daily_limit:.2f}\n"
        f"오늘 남은 사이클은 신규 매수 판단이 멈춰요. 손절·익절·트레일링 같은 "
        f"기계적 청산은 그대로 동작.\n"
        f"확인: `/cost` · 한도 조정: `settings.yaml llm.daily_cost_limit_usd`"
    )
    _fire_once("llm_cost_limit", telegram_cfg, msg)
