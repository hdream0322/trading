from __future__ import annotations

import logging
from typing import Any

from trading_bot.kis.client import KisClient
from trading_bot.store import repo

log = logging.getLogger(__name__)


def reconcile_pending_orders(kis: KisClient) -> dict[str, int]:
    """오늘 submitted 상태인 주문들의 체결 여부를 KIS 당일 체결 조회로 확인.

    각 주문에 대해 KIS inquire-daily-ccld 결과를 받아:
      - tot_ccld_qty == ord_qty → status=filled
      - 0 < tot_ccld_qty < ord_qty → status=partial
      - tot_ccld_qty == 0 and cncl_yn=Y → status=cancelled
      - tot_ccld_qty == 0 and cncl_yn=N → 그대로 submitted 유지 (장 중일 수 있음)

    반환: {filled, partial, cancelled, checked, errors} 카운트
    """
    result = {"filled": 0, "partial": 0, "cancelled": 0, "checked": 0, "errors": 0}
    pending = repo.get_pending_orders_today()
    if not pending:
        return result

    try:
        # 주문번호를 안 주면 오늘 전체가 와서 한 번의 호출로 모두 매칭 가능
        rows = kis.inquire_daily_ccld(order_no=None)
    except Exception:
        log.exception("체결 조회 실패 — 체결 추적 스킵")
        result["errors"] = len(pending)
        return result

    by_odno: dict[str, dict[str, Any]] = {}
    for row in rows:
        odno = str(row.get("odno") or "").strip()
        if odno:
            by_odno[odno] = row

    for p in pending:
        result["checked"] += 1
        odno = str(p.get("kis_order_no") or "").strip()
        row = by_odno.get(odno)
        if row is None:
            # 아직 체결 조회에 안 잡힐 수 있음 (매우 짧은 지연). 다음 사이클에 재시도.
            continue
        try:
            ord_qty = int(float(row.get("ord_qty") or 0))
            tot_ccld = int(float(row.get("tot_ccld_qty") or 0))
            cncl_yn = str(row.get("cncl_yn") or "N").upper()
            avg_price_raw = row.get("avg_prvs") or row.get("ccld_unpr")
            avg_price = int(float(avg_price_raw)) if avg_price_raw else None
        except (ValueError, TypeError):
            log.warning("체결 행 파싱 실패 %s: %s", odno, row)
            result["errors"] += 1
            continue

        if cncl_yn == "Y" and tot_ccld == 0:
            repo.update_order_status(
                order_id=p["id"],
                status="cancelled",
                reason="KIS 체결 조회 결과 취소됨",
            )
            result["cancelled"] += 1
            log.info("주문 취소 확인 [%s %s] %s", p["code"], p["name"], odno)
            continue

        if tot_ccld >= ord_qty and ord_qty > 0:
            repo.update_order_status(
                order_id=p["id"],
                status="filled",
                reason=f"KIS 체결 확인 {tot_ccld}/{ord_qty}",
                price=avg_price,
            )
            result["filled"] += 1
            log.info("주문 체결 확인 [%s %s] %s @ %s원",
                     p["code"], p["name"], odno, avg_price)
            continue

        if 0 < tot_ccld < ord_qty:
            repo.update_order_status(
                order_id=p["id"],
                status="partial",
                reason=f"부분 체결 {tot_ccld}/{ord_qty}",
                price=avg_price,
            )
            result["partial"] += 1
            log.warning("주문 부분 체결 [%s %s] %s %d/%d",
                        p["code"], p["name"], odno, tot_ccld, ord_qty)
            continue

        # tot_ccld_qty == 0, 미취소 → 미체결 대기 상태
        log.debug("주문 대기 중 [%s %s] %s", p["code"], p["name"], odno)

    return result
