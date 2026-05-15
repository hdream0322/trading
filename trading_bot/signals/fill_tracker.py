from __future__ import annotations

import logging
from typing import Any

from trading_bot.config import TelegramConfig
from trading_bot.kis.client import KisClient
from trading_bot.notify import telegram
from trading_bot.store import repo

log = logging.getLogger(__name__)


def _fmt_won(n: int | float | None) -> str:
    if n is None:
        return "-"
    try:
        return f"{int(n):,}원"
    except (ValueError, TypeError):
        return str(n)


def _notify_fill(
    tg: TelegramConfig | None,
    side: str,
    code: str,
    name: str,
    qty: int,
    avg_price: int | None,
    status: str,
    reason: str | None = None,
) -> None:
    if tg is None:
        return
    side_ko = "매수" if side == "buy" else "매도" if side == "sell" else side
    icon = {"filled": "✅", "partial": "🟡", "cancelled": "⛔"}.get(status, "ℹ️")
    head = {
        "filled": f"{icon} *{side_ko} 체결 완료*",
        "partial": f"{icon} *{side_ko} 부분 체결*",
        "cancelled": f"{icon} *{side_ko} 주문 취소*",
    }.get(status, f"{icon} {side_ko} {status}")
    lines = [
        head,
        f"{name} (`{code}`)",
        f"{qty}주 @ {_fmt_won(avg_price)}",
    ]
    if avg_price and qty:
        lines.append(f"체결금액 {_fmt_won(avg_price * qty)}")
    if reason:
        lines.append(f"_{reason}_")
    try:
        telegram.send(tg, "\n".join(lines))
    except Exception:
        log.exception("체결 알림 전송 실패")


def _guess_cancel_reason(
    kis: KisClient,
    code: str,
    name: str,
    order_price: int | None,
) -> str:
    """미체결 자동 취소 사유 추정 — 현재가 vs 주문 당시가 비교."""
    base = "시장가 주문이지만 호가 부족·상한가·거래정지 등으로 30초 내 체결 실패. 다음 사이클에서 재판단합니다."
    if not order_price or order_price <= 0:
        return base
    try:
        price_data = kis.get_price(code)
        cur_price = int(float(price_data.get("stck_prpr") or 0))
        if cur_price <= 0:
            return base
        gap_pct = (cur_price - order_price) / order_price * 100
        sign = "+" if gap_pct >= 0 else ""
        return (
            f"호가 부족 추정 (주문 당시 {_fmt_won(order_price)} → 현재 {_fmt_won(cur_price)}, "
            f"{sign}{gap_pct:.1f}%)"
        )
    except Exception:
        log.debug("미체결 사유 추정 위한 시세 조회 실패 [%s]", code)
        return base


def reconcile_pending_orders(
    kis: KisClient,
    auto_cancel_unfilled_buys: bool = True,
    telegram_cfg: TelegramConfig | None = None,
) -> dict[str, int]:
    """오늘 submitted 상태인 주문들의 체결 여부를 KIS 당일 체결 조회로 확인.

    각 주문에 대해 KIS inquire-daily-ccld 결과를 받아:
      - tot_ccld_qty == ord_qty → status=filled
      - 0 < tot_ccld_qty < ord_qty → status=partial (그대로 두고 다음 사이클 재확인)
      - tot_ccld_qty == 0 and cncl_yn=Y → status=cancelled
      - tot_ccld_qty == 0 and cncl_yn=N and side=buy → 자동 취소 시도 (매수)
      - tot_ccld_qty == 0 and cncl_yn=N and side=sell → 그대로 대기 (판매는 안전을 위해)

    auto_cancel_unfilled_buys=True 면 미체결 매수를 KIS cancel_order 로 취소.

    반환: {filled, partial, cancelled, auto_cancelled, checked, errors, unconfirmed} 카운트
    """
    result = {
        "filled": 0,
        "partial": 0,
        "cancelled": 0,
        "auto_cancelled": 0,
        "checked": 0,
        "errors": 0,
        "unconfirmed": 0,
    }
    pending = repo.get_pending_orders_today()
    if not pending:
        return result

    try:
        # 주문번호를 안 주면 오늘 전체가 와서 한 번의 호출로 모두 매칭 가능
        rows = kis.inquire_daily_ccld(order_no=None)
    except Exception:
        n = len(pending)
        log.exception("체결 조회 실패 — 체결 추적 스킵")
        result["errors"] = n
        result["unconfirmed"] = n
        # [C6] 체결 추적 실패 시 텔레그램 알림
        if telegram_cfg is not None:
            try:
                telegram.send(
                    telegram_cfg,
                    f"🚨 *체결 확인 실패* — {n}건 미확인. 다음 점검에서 재시도.",
                )
            except Exception:
                log.exception("체결 확인 실패 알림 전송 실패")
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
            rmn_qty = int(float(row.get("rmn_qty") or 0))
            cncl_yn = str(row.get("cncl_yn") or "N").upper()
            avg_price_raw = row.get("avg_prvs") or row.get("ccld_unpr")
            avg_price = int(float(avg_price_raw)) if avg_price_raw else None
            krx_fwdg_orgno = str(row.get("ord_gno_brno") or "").strip()
        except (ValueError, TypeError):
            log.warning("체결 행 파싱 실패 %s: %s", odno, row)
            result["errors"] += 1
            continue

        side = str(p.get("side") or "").lower()
        # 이전 상태 확인 (부분→완전 체결 전이 감지용)
        prev_status = str(p.get("status") or "submitted").lower()

        if cncl_yn == "Y" and tot_ccld == 0:
            repo.update_order_status(
                order_id=p["id"],
                status="cancelled",
                reason="KIS 체결 조회 결과 취소됨",
            )
            result["cancelled"] += 1
            log.info("주문 취소 확인 [%s %s] %s", p["code"], p["name"], odno)
            _notify_fill(
                telegram_cfg, side, p["code"], p["name"], ord_qty,
                None, "cancelled", "KIS 체결 조회 결과 취소",
            )
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
            # [C8] 부분→완전 체결 전이 시 잔량 완료 명시
            extra_reason: str | None = None
            if prev_status == "partial":
                extra_reason = f"잔량 {tot_ccld}주까지 체결 완료 (총 {ord_qty}주)"
            _notify_fill(
                telegram_cfg, side, p["code"], p["name"], tot_ccld,
                avg_price, "filled", extra_reason,
            )
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
            _notify_fill(
                telegram_cfg, side, p["code"], p["name"], tot_ccld,
                avg_price, "partial", f"{tot_ccld}/{ord_qty}주 체결, 나머지 대기",
            )
            continue

        # tot_ccld_qty == 0, 미취소 → 미체결 대기 상태
        if auto_cancel_unfilled_buys and side == "buy":
            # 매수 미체결 — 가격이 너무 빨리 움직였거나 상/하한가 근접.
            # 자동 취소 후 다음 사이클에서 재판단. 체결 조직번호 없으면 취소 불가.
            if not krx_fwdg_orgno:
                log.warning(
                    "미체결 매수 자동취소 불가 (ord_gno_brno 없음) [%s %s] %s",
                    p["code"], p["name"], odno,
                )
                continue
            cancel_qty = rmn_qty if rmn_qty > 0 else (ord_qty - tot_ccld)
            try:
                kis.cancel_order(
                    order_no=odno,
                    krx_fwdg_ord_orgno=krx_fwdg_orgno,
                    qty=max(cancel_qty, 1),
                )
                # [A2] 미체결 사유 추정 — 현재가 vs 주문가 비교
                order_price = int(p.get("price") or 0)
                cancel_reason = _guess_cancel_reason(
                    kis, p["code"], p["name"], order_price if order_price > 0 else None
                )
                repo.update_order_status(
                    order_id=p["id"],
                    status="cancelled",
                    reason=f"30초 내 미체결 — 자동 취소",
                )
                result["auto_cancelled"] += 1
                log.warning(
                    "미체결 매수 자동 취소 [%s %s] %s (잔량 %d주) 사유: %s",
                    p["code"], p["name"], odno, cancel_qty, cancel_reason,
                )
                _notify_fill(
                    telegram_cfg, side, p["code"], p["name"], cancel_qty,
                    None, "cancelled", cancel_reason,
                )
            except Exception as exc:
                log.exception(
                    "미체결 매수 자동 취소 실패 [%s %s] %s",
                    p["code"], p["name"], odno,
                )
                repo.insert_error(
                    component="fill_tracker",
                    message=f"cancel failed {odno}: {exc}",
                )
                result["errors"] += 1
        else:
            # 판매는 자동 취소하지 않음 (손절/청산이면 계속 기다려야 함)
            log.debug("판매 주문 대기 중 [%s %s] %s", p["code"], p["name"], odno)

    return result
