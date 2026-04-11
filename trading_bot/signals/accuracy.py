"""사후 정확도 트래킹 (v0.6.0).

과거 signal 의 판단이 실제로 맞았는지 **N 거래일 후 종가** 로 검증해
`signals.realized_return_pct` 컬럼에 기록. 매일 장 마감 후 크론으로 실행.

확인 정의:
  - buy 판단: realized_return_pct >= +1% 면 적중
  - sell 판단: realized_return_pct <= -1% 면 적중
  - hold 는 집계 제외 (방향성 없음)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from trading_bot.kis.client import KisClient
from trading_bot.store import repo
from trading_bot.utils.calendar_kr import is_trading_day

log = logging.getLogger(__name__)


FORWARD_TRADING_DAYS = 5


def _cutoff_iso(forward_days: int) -> str:
    """forward_days 거래일 이전의 signal 부터가 평가 대상.

    오늘부터 거래일을 거꾸로 forward_days 만큼 세어 나온 날짜의 자정 ISO.
    이보다 오래된 signal 은 평가 가능 (이미 N거래일 경과).
    """
    today = date.today()
    cursor = today
    counted = 0
    while counted < forward_days:
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            counted += 1
    return datetime.combine(cursor, datetime.min.time()).isoformat(timespec="seconds")


def _pick_forward_close(
    kis: KisClient,
    code: str,
    signal_date: date,
    forward_days: int,
) -> float | None:
    """signal 발생일로부터 forward_days 거래일 뒤의 종가를 찾는다.

    KIS `get_daily_ohlcv` 는 최근 N 영업일을 넘겨주므로 충분히 넉넉히 받아
    signal_date 이후 거래일을 순서대로 세어 `forward_days` 번째 종가를 리턴.
    """
    try:
        # 충분한 여유분 — 최근 60일(휴장 고려) 받아서 slice
        ohlcv = kis.get_daily_ohlcv(code, days=60)
    except Exception as exc:
        log.warning("accuracy: %s ohlcv 조회 실패: %s", code, exc)
        return None

    # 오래된 → 최신 순. signal_date 이후 캔들만 훑는다.
    past_signal = False
    count = 0
    for c in ohlcv:
        try:
            cdate = datetime.strptime(c["date"], "%Y%m%d").date()
        except ValueError:
            continue
        if not past_signal:
            if cdate > signal_date:
                past_signal = True
                count = 1
                if count == forward_days:
                    return float(c["close"])
            continue
        count += 1
        if count == forward_days:
            return float(c["close"])
    return None


def evaluate_pending_signals(
    kis: KisClient,
    forward_days: int = FORWARD_TRADING_DAYS,
) -> dict[str, int]:
    """평가 대기 중인 signal 들에 대해 forward return 을 계산해 DB 업데이트.

    반환: {evaluated, skipped, errors} 카운트.
    """
    result = {"evaluated": 0, "skipped": 0, "errors": 0}
    cutoff = _cutoff_iso(forward_days)
    pending = repo.get_signals_awaiting_eval(cutoff)
    if not pending:
        log.info("사후 정확도 평가: 대기 signal 없음")
        return result

    # 같은 종목에 여러 signal 있으면 OHLCV 캐시 활용
    ohlcv_cache: dict[str, list[dict[str, Any]]] = {}

    for p in pending:
        code = p["code"]
        try:
            signal_ts = p["ts"]
            signal_date = datetime.fromisoformat(signal_ts).date()
        except Exception:
            result["errors"] += 1
            continue

        # signal 발생일의 종가를 베이스로 사용 (KIS 일봉은 장중 발생 시그널의 그날 종가까지 포함)
        if code not in ohlcv_cache:
            try:
                ohlcv_cache[code] = kis.get_daily_ohlcv(code, days=60)
            except Exception as exc:
                log.warning("accuracy: %s ohlcv 조회 실패: %s", code, exc)
                result["errors"] += 1
                continue
        ohlcv = ohlcv_cache[code]

        # base close = signal_date 의 종가 (또는 그 이전 가장 가까운 거래일)
        base_close: float | None = None
        forward_close: float | None = None
        past_signal = False
        count = 0
        for c in ohlcv:
            try:
                cdate = datetime.strptime(c["date"], "%Y%m%d").date()
            except ValueError:
                continue
            if cdate <= signal_date:
                base_close = float(c["close"])  # 계속 덮어써서 마지막 값이 기준가
                continue
            if not past_signal:
                past_signal = True
                count = 1
            else:
                count += 1
            if count == forward_days:
                forward_close = float(c["close"])
                break

        if base_close is None or forward_close is None or base_close <= 0:
            result["skipped"] += 1
            continue

        realized_pct = (forward_close - base_close) / base_close * 100.0
        repo.update_signal_forward_return(
            signal_id=p["id"],
            realized_return_pct=realized_pct,
            evaluated_at=datetime.now().isoformat(timespec="seconds"),
        )
        result["evaluated"] += 1
        log.info(
            "사후 평가 [%s %s] %s conf=%s → %+0.2f%% (base=%.0f, fwd=%.0f)",
            p["code"], p.get("name", ""), p["decision"], p.get("confidence"),
            realized_pct, base_close, forward_close,
        )

    log.info("사후 정확도 평가 결과: %s", result)
    return result
