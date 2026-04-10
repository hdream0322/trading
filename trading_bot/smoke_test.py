from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from trading_bot.config import load_settings
from trading_bot.kis.auth import get_access_token
from trading_bot.kis.client import KisClient
from trading_bot.logging_setup import setup_logging
from trading_bot.notify import telegram
from trading_bot.store.db import init_db

log = logging.getLogger("smoke_test")


def main() -> int:
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    setup_logging(level="INFO", log_dir=log_dir)
    log.info("==== Stage 1 Smoke Test 시작 ====")

    settings = load_settings()
    log.info("KIS 모드: %s", settings.kis.mode)
    log.info("유니버스: %d 종목", len(settings.universe))

    init_db()

    log.info("KIS 거래용 토큰 발급/캐시 검증 (mode=%s)", settings.kis.mode)
    trade_token = get_access_token(settings.kis)
    log.info("거래용 토큰 OK (앞 12자: %s...)", trade_token[:12])
    if settings.kis_quote is not settings.kis:
        log.info("KIS 시세용 토큰 발급/캐시 검증 (live 서버 강제)")
        quote_token = get_access_token(settings.kis_quote)
        log.info("시세용 토큰 OK (앞 12자: %s...)", quote_token[:12])

    lines: list[str] = []
    lines.append(f"*KIS 봇 Smoke Test* — {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"거래 모드: `{settings.kis.mode}`  /  시세 서버: `{settings.kis_quote.mode}`")
    lines.append("")

    with KisClient(settings.kis, settings.kis_quote) as kis:
        try:
            balance = kis.get_balance()
            summary = balance.get("summary", {})
            holdings = balance.get("holdings", [])
            lines.append("*잔고 요약*")
            lines.append(f"- 예수금총금액: {summary.get('dnca_tot_amt', 'N/A')}")
            lines.append(f"- 총평가금액: {summary.get('tot_evlu_amt', 'N/A')}")
            lines.append(f"- 보유종목: {len(holdings)}개")
            log.info("잔고 조회 OK: 보유 %d 종목", len(holdings))
        except Exception as exc:
            log.exception("잔고 조회 실패")
            lines.append(f"잔고 조회 실패: `{exc}`")

        lines.append("")
        lines.append("*시세 조회*")
        for item in settings.universe:
            code, name = item["code"], item["name"]
            try:
                price = kis.get_price(code)
                cur = price.get("stck_prpr", "?")
                chg_rate = price.get("prdy_ctrt", "?")
                lines.append(f"- {name} ({code}): {cur}원 ({chg_rate}%)")
                log.info("시세 OK %s %s: %s원 (%s%%)", code, name, cur, chg_rate)
            except Exception as exc:
                log.exception("시세 조회 실패: %s", code)
                lines.append(f"- {name} ({code}): 실패 `{exc}`")

    msg = "\n".join(lines)
    ok = telegram.send(settings.telegram, msg)
    if ok:
        log.info("Telegram 전송 완료")
    else:
        log.warning("Telegram 전송 실패")
    log.info("==== Stage 1 Smoke Test 종료 ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
