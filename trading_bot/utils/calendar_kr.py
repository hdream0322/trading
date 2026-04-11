from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
HOLIDAYS_FILE = ROOT / "config" / "market_holidays.yaml"

_HOLIDAYS_CACHE: set[date] | None = None


def _load_holidays() -> set[date]:
    if not HOLIDAYS_FILE.exists():
        log.warning("휴장일 파일 없음: %s (주말만 차단)", HOLIDAYS_FILE)
        return set()
    raw = yaml.safe_load(HOLIDAYS_FILE.read_text(encoding="utf-8")) or {}
    dates: set[date] = set()
    for year_holidays in raw.values():
        if not year_holidays:
            continue
        for entry in year_holidays:
            try:
                dates.add(date.fromisoformat(entry["date"]))
            except (KeyError, ValueError) as exc:
                log.warning("휴장일 항목 파싱 실패: %s (%s)", entry, exc)
    return dates


def _holidays() -> set[date]:
    global _HOLIDAYS_CACHE
    if _HOLIDAYS_CACHE is None:
        _HOLIDAYS_CACHE = _load_holidays()
    return _HOLIDAYS_CACHE


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() >= 5:  # 토/일
        return False
    if d in _holidays():
        return False
    return True


def is_market_open_now(open_str: str, close_str: str) -> bool:
    now = datetime.now()
    if not is_trading_day(now.date()):
        return False
    open_t = time.fromisoformat(open_str)
    close_t = time.fromisoformat(close_str)
    return open_t <= now.time() <= close_t


def upcoming_holidays(days_ahead: int = 14) -> list[date]:
    """오늘부터 days_ahead 일 이내의 등록된 휴장일 목록 (정렬).

    주간 리마인더에서 "이번 주/다음 주 휴장일이 있는지" 확인용.
    """
    from datetime import timedelta
    today = date.today()
    end = today + timedelta(days=days_ahead)
    result = [d for d in _holidays() if today <= d <= end]
    return sorted(result)


def reload_holidays() -> int:
    """휴장일 캐시 갱신. YAML 파일이 바뀌었을 때 호출.

    반환: 로드된 휴장일 개수
    """
    global _HOLIDAYS_CACHE
    _HOLIDAYS_CACHE = _load_holidays()
    return len(_HOLIDAYS_CACHE)
