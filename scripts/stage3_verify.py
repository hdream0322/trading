"""Stage 3 end-to-end verification script.

Exercises: 잔고 조회 → 리스크 매니저 판정 → 1주 시장가 매수(모의) → DB 기록 → 텔레그램 → 재조회.
모의 모드에서만 실행 허용.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from trading_bot.config import load_settings
from trading_bot.kis.client import KisClient
from trading_bot.logging_setup import setup_logging
from trading_bot.notify import telegram
from trading_bot.risk.manager import RiskManager
from trading_bot.store import repo
from trading_bot.store.db import init_db


def main() -> int:
    setup_logging(level="INFO", log_dir=Path("logs"))
    s = load_settings()
    print(f"[1] KIS 모드: {s.kis.mode}")
    assert s.kis.mode == "paper", "paper 모드가 아닙니다 — 중단"

    init_db()
    risk = RiskManager(s)

    with KisClient(s.kis, s.kis_quote) as kis:
        print("\n[2] 잔고 조회")
        bal = kis.get_balance()
        bs = bal["summary"]
        holdings = KisClient.normalize_holdings(bal["holdings"])

        tot_eval = bs.get("tot_evlu_amt")
        dnca = bs.get("dnca_tot_amt")
        daily_pct = bs.get("asst_icdc_erng_rt")
        print(f"    총평가: {tot_eval}원")
        print(f"    예수금: {dnca}원")
        print(f"    전일 대비: {daily_pct}%")
        print(f"    보유 종목: {len(holdings)}개")
        for code, p in holdings.items():
            pname = p["name"]
            pqty = p["qty"]
            pavg = p["avg_price"]
            print(f"      - {pname} ({code}) {pqty}주 @ {pavg:.0f}")

        print("\n[3] 삼성전자 현재가")
        price_raw = kis.get_price("005930")
        cur = int(float(price_raw["stck_prpr"]))
        print(f"    {cur}원")

        print("\n[4] 리스크 매니저 판정 (buy)")
        rd = risk.check(
            side="buy",
            code="005930",
            name="삼성전자",
            current_price=cur,
            balance_summary=bs,
            holdings=holdings,
        )
        print(f"    allowed={rd.allowed} qty={rd.qty}")
        print(f"    reason: {rd.reason}")

        print("\n[5] 테스트용 1주 시장가 매수 주문 제출 (risk 판정과 별개로 강제)")
        try:
            order = kis.place_market_order("005930", "buy", 1)
            ord_no = order["order_no"]
            ord_time = order["order_time"]
            print(f"    ✅ 주문번호: {ord_no}")
            print(f"    주문시각: {ord_time}")

            order_id = repo.insert_order(
                ts=datetime.now().isoformat(timespec="seconds"),
                code="005930",
                name="삼성전자",
                side="buy",
                qty=1,
                price=None,
                mode=s.kis.mode,
                kis_order_no=ord_no,
                status="submitted",
                raw_response=json.dumps(order["raw"], ensure_ascii=False)[:2000],
                reason="stage3 verification test (1 share)",
            )
            print(f"    DB order_id={order_id}")

            msg = (
                "*[STAGE 3 TEST]* 삼성전자 1주 시장가 매수 제출\n"
                f"모드: `{s.kis.mode}`\n"
                f"주문번호: `{ord_no}`\n"
                f"참고가: {cur}원"
            )
            telegram.send(s.telegram, msg)

            print("\n[6] 제출 후 잔고 재조회 (포지션 반영 확인)")
            bal2 = kis.get_balance()
            holdings2 = KisClient.normalize_holdings(bal2["holdings"])
            if "005930" in holdings2:
                p = holdings2["005930"]
                print(f"    ✅ 포지션 반영됨: {p['qty']}주 @ {p['avg_price']:.0f}")
            else:
                print("    ⏳ 아직 체결 안 됨 (장외 시간이라 대기 중일 가능성)")
            return 0
        except Exception as exc:
            print(f"    ❌ 주문 실패: {type(exc).__name__}")
            print(f"    {exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
