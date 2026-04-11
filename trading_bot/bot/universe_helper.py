"""universe.json 헬퍼 — 섹터 백필 + 조회 유틸.

universe 아이템은 `{code, name, sector?}` 구조. sector 는 KIS `inquire-price`
응답의 `bstp_kor_isnm` 필드(업종 한글명) 에서 가져오고, 섹터 분산 리스크
게이트에서 사용.
"""
from __future__ import annotations

import logging
from typing import Any

from trading_bot.config import save_universe_override
from trading_bot.kis.client import KisClient

log = logging.getLogger(__name__)


def backfill_sectors(
    universe: list[dict[str, Any]],
    kis: KisClient,
) -> int:
    """universe 항목 중 sector 가 비어있는 것들에 대해 KIS 로 조회해서 채운다.

    in-place 로 universe 리스트를 수정하고, 변경이 있었으면 universe.json 에 저장.
    네트워크 실패는 무시 (빈 sector 로 남겨두면 게이트가 우회됨).

    반환: 새로 백필된 종목 수
    """
    filled = 0
    for item in universe:
        if str(item.get("sector", "")).strip():
            continue
        code = str(item.get("code", ""))
        if not code:
            continue
        sector = kis.get_stock_sector(code)
        if sector:
            item["sector"] = sector
            filled += 1
            log.info("섹터 백필: %s (%s) → %s", item.get("name", ""), code, sector)
        else:
            log.debug("섹터 조회 결과 없음: %s", code)

    if filled > 0:
        try:
            save_universe_override(universe)
            log.info("섹터 백필 완료 — %d개 종목 업데이트", filled)
        except Exception:
            log.exception("universe.json 섹터 백필 저장 실패")
    return filled


def code_to_sector_map(universe: list[dict[str, Any]]) -> dict[str, str]:
    """universe 리스트에서 {code: sector} 맵 생성. sector 없는 항목은 제외."""
    return {
        str(item["code"]): str(item["sector"]).strip()
        for item in universe
        if str(item.get("sector", "")).strip()
    }


def count_holdings_by_sector(
    holdings: dict[str, dict[str, Any]],
    sector_map: dict[str, str],
) -> dict[str, int]:
    """보유 중인 종목들을 섹터별로 카운트. sector_map 에 없는 종목은 집계 제외."""
    counts: dict[str, int] = {}
    for code in holdings.keys():
        sector = sector_map.get(code)
        if not sector:
            continue
        counts[sector] = counts.get(sector, 0) + 1
    return counts
