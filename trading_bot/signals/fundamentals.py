"""Stage 10 — 펀더멘털 데이터 연동 오케스트레이터.

KIS 재무비율 API → SQLite 캐시 → 리스크 게이트 / LLM 입력 전달.
모든 함수는 실패 시 None 반환 (보조 입력 원칙 — 사이클이 멈추면 안 됨).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trading_bot.store import repo

log = logging.getLogger(__name__)


@dataclass
class FundamentalData:
    """단일 종목의 재무지표 캐시 레코드."""

    code: str
    name: str | None
    per: float | None
    pbr: float | None
    roe: float | None
    eps: float | None
    bps: float | None
    debt_ratio: float | None
    dividend_yield: float | None
    updated_at: str


def _row_to_data(row: dict[str, Any]) -> FundamentalData:
    return FundamentalData(
        code=row["code"],
        name=row.get("name"),
        per=row.get("per"),
        pbr=row.get("pbr"),
        roe=row.get("roe"),
        eps=row.get("eps"),
        bps=row.get("bps"),
        debt_ratio=row.get("debt_ratio"),
        dividend_yield=row.get("dividend_yield"),
        updated_at=row["updated_at"],
    )


def fetch_and_cache(code: str, name: str | None, kis: Any) -> FundamentalData | None:
    """KIS API 에서 재무비율을 조회하고 DB 캐시에 저장. 실패 시 None."""
    try:
        raw = kis.get_financial_ratio(code)
    except Exception as exc:
        log.warning("%s 재무비율 API 조회 실패: %s", code, exc)
        return None

    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        repo.upsert_fundamentals_cache(
            code=code,
            name=name,
            per=raw.get("per"),
            pbr=raw.get("pbr"),
            roe=raw.get("roe"),
            eps=raw.get("eps"),
            bps=raw.get("bps"),
            debt_ratio=raw.get("debt_ratio"),
            dividend_yield=raw.get("dividend_yield"),
            updated_at=now_iso,
        )
    except Exception as exc:
        log.warning("%s 재무비율 캐시 저장 실패: %s", code, exc)

    return FundamentalData(
        code=code,
        name=name,
        per=raw.get("per"),
        pbr=raw.get("pbr"),
        roe=raw.get("roe"),
        eps=raw.get("eps"),
        bps=raw.get("bps"),
        debt_ratio=raw.get("debt_ratio"),
        dividend_yield=raw.get("dividend_yield"),
        updated_at=now_iso,
    )


def get_cached(code: str, max_age_days: int = 10) -> FundamentalData | None:
    """DB 캐시에서 재무지표 조회. 없거나 만료(>max_age_days)면 None."""
    row = repo.get_fundamentals_cache(code)
    if not row:
        return None
    try:
        updated = datetime.fromisoformat(row["updated_at"])
        if datetime.now() - updated > timedelta(days=max_age_days):
            log.debug("%s 재무지표 캐시 만료 (%s)", code, row["updated_at"])
            return None
    except (ValueError, TypeError):
        return None
    return _row_to_data(row)


def get_or_fetch(
    code: str,
    name: str | None,
    kis: Any,
    max_age_days: int = 10,
) -> FundamentalData | None:
    """캐시 히트면 캐시, 미스/만료면 API 조회 후 캐시 갱신. 실패 시 None."""
    cached = get_cached(code, max_age_days)
    if cached is not None:
        return cached
    return fetch_and_cache(code, name, kis)


def refresh_universe(
    universe: list[dict[str, str]],
    kis: Any,
) -> dict[str, int]:
    """유니버스 전체 재무지표 배치 갱신. 반환: {success: N, failed: N}."""
    success = 0
    failed = 0
    for item in universe:
        code = str(item["code"])
        name = str(item.get("name", ""))
        result = fetch_and_cache(code, name, kis)
        if result is not None:
            success += 1
        else:
            failed += 1
    log.info("펀더멘털 배치 갱신 완료: 성공 %d / 실패 %d", success, failed)
    return {"success": success, "failed": failed}


def format_for_display(data: FundamentalData) -> str:
    """텔레그램 출력용 포맷."""
    name_str = f"{data.name} " if data.name else ""
    lines = [f"📊 *{name_str}(`{data.code}`) 펀더멘털*"]

    def _fmt(label: str, val: float | None, suffix: str = "", fmt: str = ".1f") -> str | None:
        if val is None:
            return None
        return f"- {label}: {val:{fmt}}{suffix}"

    items = [
        _fmt("PER", data.per, "배"),
        _fmt("PBR", data.pbr, "배", ".2f"),
        _fmt("ROE", data.roe, "%"),
        _fmt("부채비율", data.debt_ratio, "%", ".0f"),
        _fmt("EPS", data.eps, "원", ",.0f"),
        _fmt("BPS", data.bps, "원", ",.0f"),
        _fmt("배당수익률", data.dividend_yield, "%", ".2f"),
    ]
    items = [i for i in items if i is not None]
    if not items:
        lines.append("_재무지표 데이터가 없습니다._")
    else:
        lines.extend(items)

    lines.append(f"\n_갱신: {data.updated_at[:10]}_")
    return "\n".join(lines)
