"""한국 휴장일 자동 동기화.

`python-holidays` 라이브러리를 데이터 소스로 사용한다. 커뮤니티가 유지·업데이트
하므로 임시공휴일도 라이브러리 갱신 시 반영됨. 네트워크 독립적이라 외부 장애
영향 없음.

주식시장 특수 규칙 (공휴일 외):
- **연말 휴장**: 12/31 이 평일이면 휴장. `holidays.KR` 에 없어 수동 추가.

주말은 `calendar_kr` 에서 자동 차단하므로 YAML 에 저장하지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import holidays
import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
HOLIDAYS_FILE = ROOT / "config" / "market_holidays.yaml"


def fetch_kr_holidays(year: int) -> list[dict[str, str]]:
    """해당 연도의 한국 주식시장 휴장일 목록 반환.

    `[{"date": "2026-01-01", "name": "신정연휴"}, ...]` 형식, 평일만, 오름차순.
    """
    kr = holidays.country_holidays("KR", years=[year])
    results: list[dict[str, str]] = []
    for d, name in kr.items():
        if d.weekday() >= 5:  # 주말은 calendar_kr 이 차단
            continue
        results.append({"date": d.isoformat(), "name": str(name)})

    # 연말 휴장 (12/31 평일이면 추가)
    year_end = date(year, 12, 31)
    if year_end.weekday() < 5:
        iso = year_end.isoformat()
        if not any(e["date"] == iso for e in results):
            results.append({"date": iso, "name": "연말 휴장"})

    results.sort(key=lambda e: e["date"])
    return results


_HEADER_COMMENT = (
    "# 한국 주식시장 휴장일. 매주 일요일 03:30 KST 에 자동 동기화됨.\n"
    "# 데이터 소스: python-holidays 패키지 (KR) + 연말 휴장 규칙.\n"
    "# 주말은 calendar_kr 에서 자동 차단되므로 여기에 포함하지 않는다.\n"
    "# 현재 연도 블록은 동기화 시 덮어쓰기됨 — 수동 편집은 비권장.\n\n"
)


def sync_holidays_yaml(year: int) -> dict:
    """해당 연도 휴장일을 받아 YAML 의 해당 연도 블록을 덮어쓴다.

    다른 연도 블록은 유지. 반환:
    `{"year": Y, "count": N, "added": [...], "removed": [...]}`.
    """
    fetched = fetch_kr_holidays(year)
    fetched_dates = {e["date"] for e in fetched}

    raw: dict = {}
    if HOLIDAYS_FILE.exists():
        raw = yaml.safe_load(HOLIDAYS_FILE.read_text(encoding="utf-8")) or {}

    existing = raw.get(year) or []
    existing_dates = {
        e.get("date") for e in existing if isinstance(e, dict) and e.get("date")
    }

    added = sorted(fetched_dates - existing_dates)
    removed = sorted(existing_dates - fetched_dates)

    raw[year] = fetched
    body = yaml.safe_dump(
        raw, allow_unicode=True, default_flow_style=False, sort_keys=True
    )
    HOLIDAYS_FILE.write_text(_HEADER_COMMENT + body, encoding="utf-8")

    return {
        "year": year,
        "count": len(fetched),
        "added": added,
        "removed": removed,
    }
