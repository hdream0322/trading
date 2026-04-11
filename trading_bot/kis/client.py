from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from trading_bot.config import KisConfig
from trading_bot.kis.auth import get_access_token

log = logging.getLogger(__name__)


class KisClient:
    """시세(quote)와 주문/잔고(trade)를 분리해서 다룬다.

    - trade: KIS_MODE에 따라 paper/live 서버
    - quote: 항상 live 서버 (모의 서버 시세는 부분 지원이라 500 빈번)
    """

    # KIS 공식 유량 정책 (2026-04 기준):
    #   - 실전: 기본 20 req/s (신규 API 발급 후 3일간은 3 req/s 로 임시 제한)
    #   - 모의: 기본 2 req/s (신규 제한 없음)
    # 기본값은 한도 대비 안전 마진 ~10% 두고 설정:
    #   - LIVE 0.055s ≈ 18 req/s (한도 20 의 90%)
    #   - PAPER 0.55s ≈ 1.8 req/s (한도 2 의 90%)
    # 신규 API 발급 직후 3일간은 settings.yaml 의 rate_limit.live_min_interval_sec
    # 를 0.34 (≈2.9 req/s) 로 임시 조정 후 다시 복원.
    DEFAULT_MIN_INTERVAL_LIVE_SEC = 0.055
    DEFAULT_MIN_INTERVAL_PAPER_SEC = 0.55

    def __init__(
        self,
        trade_cfg: KisConfig,
        quote_cfg: KisConfig | None = None,
        live_min_interval_sec: float | None = None,
        paper_min_interval_sec: float | None = None,
    ):
        self.trade_cfg = trade_cfg
        self.quote_cfg = quote_cfg or trade_cfg
        self._live_min_interval = (
            float(live_min_interval_sec)
            if live_min_interval_sec is not None
            else self.DEFAULT_MIN_INTERVAL_LIVE_SEC
        )
        self._paper_min_interval = (
            float(paper_min_interval_sec)
            if paper_min_interval_sec is not None
            else self.DEFAULT_MIN_INTERVAL_PAPER_SEC
        )
        log.info(
            "KIS throttle 설정: live=%.3fs (≈%.1f req/s) / paper=%.3fs (≈%.1f req/s)",
            self._live_min_interval, 1.0 / self._live_min_interval,
            self._paper_min_interval, 1.0 / self._paper_min_interval,
        )
        self._trade_client = httpx.Client(base_url=trade_cfg.base_url, timeout=10)
        self._quote_client = httpx.Client(base_url=self.quote_cfg.base_url, timeout=10)
        self._last_request: dict[str, float] = {
            trade_cfg.base_url: 0.0,
            self.quote_cfg.base_url: 0.0,
        }
        self._throttle_lock = threading.Lock()

    def close(self) -> None:
        self._trade_client.close()
        if self._quote_client is not self._trade_client:
            self._quote_client.close()

    def __enter__(self) -> "KisClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _min_interval(self, cfg: KisConfig) -> float:
        return self._live_min_interval if cfg.is_live else self._paper_min_interval

    def _throttle(self, cfg: KisConfig) -> None:
        with self._throttle_lock:
            key = cfg.base_url
            elapsed = time.monotonic() - self._last_request.get(key, 0.0)
            wait = self._min_interval(cfg) - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request[key] = time.monotonic()

    def _headers(self, cfg: KisConfig, tr_id: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {get_access_token(cfg)}",
            "appkey": cfg.app_key,
            "appsecret": cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "content-type": "application/json; charset=utf-8",
        }

    def get_price(self, code: str, max_retries: int = 3) -> dict[str, Any]:
        cfg = self.quote_cfg
        tr_id = "FHKST01010100"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }

        attempt = 0
        last_err: str = ""
        while attempt < max_retries:
            attempt += 1
            self._throttle(cfg)
            resp = self._quote_client.get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                params=params,
                headers=self._headers(cfg, tr_id),
            )

            body: dict[str, Any] | None
            try:
                body = resp.json()
            except Exception:
                body = None

            if resp.status_code == 200 and body and body.get("rt_cd") == "0":
                return body["output"]

            # KIS가 500으로 응답하거나 rt_cd != 0 인 경우 바디에 원인이 들어있다.
            msg = (body or {}).get("msg1") if body else resp.text[:200]
            msg_cd = (body or {}).get("msg_cd") if body else ""
            last_err = f"status={resp.status_code} msg_cd={msg_cd} msg1={msg}"

            # "초당 거래건수 초과"는 대기 후 재시도하면 해결되는 과도성 에러.
            is_rate_limit = "초당" in (msg or "") or msg_cd in {"EGW00201", "EGW00121"}
            if is_rate_limit and attempt < max_retries:
                backoff = 0.5 * attempt
                log.warning(
                    "시세 조회 rate limit (%s), %.1f초 대기 후 재시도 %d/%d",
                    code, backoff, attempt, max_retries,
                )
                time.sleep(backoff)
                continue
            # 500이지만 바디가 없거나 다른 원인이면 한 번만 짧게 재시도
            if resp.status_code >= 500 and attempt < max_retries:
                log.warning(
                    "시세 조회 서버 오류 (%s) %s, 재시도 %d/%d",
                    code, last_err, attempt, max_retries,
                )
                time.sleep(0.3 * attempt)
                continue
            break

        raise RuntimeError(f"KIS 현재가 조회 실패 ({code}): {last_err}")

    def get_stock_sector(self, code: str) -> str:
        """종목코드의 업종 한글명 (섹터) 조회.

        `inquire-price` 응답의 `bstp_kor_isnm` 필드를 재활용.
        예: "반도체", "은행", "자동차부품".
        실패/없음 시 빈 문자열 반환 (섹터 게이트가 우회됨).
        """
        try:
            output = self.get_price(code)
            return str(output.get("bstp_kor_isnm") or "").strip()
        except Exception as exc:
            log.warning("섹터 조회 실패 %s: %s", code, exc)
            return ""

    def get_stock_name(self, code: str, max_retries: int = 3) -> str:
        """종목코드로 한국어 종목명을 조회한다.

        `inquire-price` 응답에는 종목명이 없어서(업종명 bstp_kor_isnm 만 내려옴)
        별도의 `search-stock-info` 엔드포인트(TR_ID CTPF1604R)를 쓴다.
        약식명(`prdt_abrv_name`, 예: "기아") 을 우선 반환하고, 없으면 정식명
        (`prdt_name`, 예: "기아보통주") 으로 폴백한다.
        """
        cfg = self.quote_cfg
        tr_id = "CTPF1604R"
        params = {
            "PRDT_TYPE_CD": "300",  # 300 = 국내주식
            "PDNO": code,
        }

        attempt = 0
        last_err = ""
        while attempt < max_retries:
            attempt += 1
            self._throttle(cfg)
            resp = self._quote_client.get(
                "/uapi/domestic-stock/v1/quotations/search-stock-info",
                params=params,
                headers=self._headers(cfg, tr_id),
            )

            body: dict[str, Any] | None
            try:
                body = resp.json()
            except Exception:
                body = None

            if resp.status_code == 200 and body and body.get("rt_cd") == "0":
                output = body.get("output") or {}
                name = (
                    str(output.get("prdt_abrv_name") or "").strip()
                    or str(output.get("prdt_name") or "").strip()
                )
                if name:
                    return name
                last_err = "output 에 종목명이 비어 있음"
                break

            msg = (body or {}).get("msg1") if body else resp.text[:200]
            msg_cd = (body or {}).get("msg_cd") if body else ""
            last_err = f"status={resp.status_code} msg_cd={msg_cd} msg1={msg}"

            is_rate_limit = "초당" in (msg or "") or msg_cd in {"EGW00201", "EGW00121"}
            if is_rate_limit and attempt < max_retries:
                backoff = 0.5 * attempt
                log.warning(
                    "종목명 조회 rate limit (%s), %.1f초 대기 후 재시도 %d/%d",
                    code, backoff, attempt, max_retries,
                )
                time.sleep(backoff)
                continue
            if resp.status_code >= 500 and attempt < max_retries:
                log.warning(
                    "종목명 조회 서버 오류 (%s) %s, 재시도 %d/%d",
                    code, last_err, attempt, max_retries,
                )
                time.sleep(0.3 * attempt)
                continue
            break

        raise RuntimeError(f"KIS 종목명 조회 실패 ({code}): {last_err}")

    def get_daily_ohlcv(self, code: str, days: int = 30, max_retries: int = 3) -> list[dict[str, Any]]:
        """최근 N 영업일의 일봉 OHLCV. 결과는 오래된 순 → 최신 순 정렬.

        KIS `inquire-daily-itemchartprice` (TR_ID FHKST03010100, 수정주가).
        """
        cfg = self.quote_cfg
        tr_id = "FHKST03010100"
        today = datetime.now()
        # 휴장일 여유를 두기 위해 넉넉하게 캘린더일로 요청.
        start = today - timedelta(days=max(days * 3, 100))
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",  # 수정주가
        }

        last_err = ""
        for attempt in range(1, max_retries + 1):
            self._throttle(cfg)
            resp = self._quote_client.get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                params=params,
                headers=self._headers(cfg, tr_id),
            )
            body: dict[str, Any] | None
            try:
                body = resp.json()
            except Exception:
                body = None

            if resp.status_code == 200 and body and body.get("rt_cd") == "0":
                raw = body.get("output2") or []
                candles: list[dict[str, Any]] = []
                # KIS는 최신 → 과거 순으로 반환. 오래된 순으로 뒤집는다.
                for row in reversed(raw):
                    if not row or not row.get("stck_bsop_date"):
                        continue
                    try:
                        candles.append({
                            "date": row["stck_bsop_date"],
                            "open": float(row.get("stck_oprc") or 0),
                            "high": float(row.get("stck_hgpr") or 0),
                            "low": float(row.get("stck_lwpr") or 0),
                            "close": float(row.get("stck_clpr") or 0),
                            "volume": float(row.get("acml_vol") or 0),
                        })
                    except (ValueError, TypeError):
                        continue
                return candles[-days:]

            msg = (body or {}).get("msg1", resp.text[:200] if body is None else "")
            msg_cd = (body or {}).get("msg_cd", "") if body else ""
            last_err = f"status={resp.status_code} msg_cd={msg_cd} msg1={msg}"

            is_rate_limit = "초당" in (msg or "") or msg_cd in {"EGW00201", "EGW00121"}
            if (is_rate_limit or resp.status_code >= 500) and attempt < max_retries:
                wait = 0.5 * attempt
                log.warning("일봉 조회 재시도 (%s) %s after %.1fs", code, last_err, wait)
                time.sleep(wait)
                continue
            break

        raise RuntimeError(f"KIS 일봉 조회 실패 ({code}): {last_err}")

    def get_hashkey(self, body: dict[str, Any], max_retries: int = 3) -> str:
        """주문 body에 대한 hashkey 발급. 주문 POST에 필수 헤더."""
        cfg = self.trade_cfg
        last_err = ""
        for attempt in range(1, max_retries + 1):
            self._throttle(cfg)
            resp = self._trade_client.post(
                "/uapi/hashkey",
                json=body,
                headers={
                    "content-type": "application/json; charset=utf-8",
                    "appkey": cfg.app_key,
                    "appsecret": cfg.app_secret,
                },
            )
            try:
                data = resp.json()
            except Exception:
                data = None

            if resp.status_code == 200 and data:
                h = data.get("HASH") or data.get("hash")
                if h:
                    return h

            msg = (data or {}).get("msg1", resp.text[:200] if data is None else "")
            last_err = f"status={resp.status_code} msg1={msg}"

            if "초당" in (msg or "") and attempt < max_retries:
                wait = 0.5 * attempt
                log.warning("hashkey rate limit, %.1fs 후 재시도 %d/%d", wait, attempt, max_retries)
                time.sleep(wait)
                continue
            break
        raise RuntimeError(f"hashkey 발급 실패: {last_err}")

    def place_market_order(self, code: str, side: str, qty: int) -> dict[str, Any]:
        """국내주식 시장가 주문. side: 'buy' | 'sell'. 주문 모드(paper/live)는 trade_cfg.

        반환: {order_no, order_time, raw}
        """
        if side not in {"buy", "sell"}:
            raise ValueError(f"side는 'buy' 또는 'sell' (입력: {side!r})")
        if qty < 1:
            raise ValueError(f"주문 수량은 1 이상 (입력: {qty})")

        cfg = self.trade_cfg
        if cfg.is_live:
            tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"
        else:
            tr_id = "VTTC0802U" if side == "buy" else "VTTC0801U"

        body = {
            "CANO": cfg.account_no,
            "ACNT_PRDT_CD": cfg.account_product_cd,
            "PDNO": code,
            "ORD_DVSN": "01",   # 01 = 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        }

        # 게이트웨이 rate-limit만 한정해서 재시도 (주문 엔진 도달 전 거절이라 재실행 안전).
        # 실제 주문 처리 단계 에러(잔고부족, 시장 마감 등)는 절대 재시도 금지.
        max_retries = 3
        last_err = ""
        for attempt in range(1, max_retries + 1):
            hashkey = self.get_hashkey(body)
            self._throttle(cfg)
            resp = self._trade_client.post(
                "/uapi/domestic-stock/v1/trading/order-cash",
                json=body,
                headers={
                    "authorization": f"Bearer {get_access_token(cfg)}",
                    "appkey": cfg.app_key,
                    "appsecret": cfg.app_secret,
                    "tr_id": tr_id,
                    "custtype": "P",
                    "hashkey": hashkey,
                    "content-type": "application/json; charset=utf-8",
                },
            )
            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(
                    f"KIS 주문 응답 파싱 실패: status={resp.status_code} body={resp.text[:300]}"
                )

            if resp.status_code == 200 and data.get("rt_cd") == "0":
                output = data.get("output") or {}
                return {
                    "order_no": str(output.get("ODNO", "")),
                    "order_time": str(output.get("ORD_TMD", "")),
                    "raw": data,
                }

            msg = data.get("msg1", "")
            msg_cd = data.get("msg_cd", "")
            last_err = f"status={resp.status_code} msg_cd={msg_cd} msg1={msg}"

            # rate limit만 재시도 대상. 그 외는 바로 예외.
            if "초당" in msg and attempt < max_retries:
                wait = 0.5 * attempt
                log.warning(
                    "주문 rate limit (%s %s %d), %.1fs 후 재시도 %d/%d",
                    code, side, qty, wait, attempt, max_retries,
                )
                time.sleep(wait)
                continue
            break

        raise RuntimeError(f"KIS 주문 실패 ({code} {side} {qty}): {last_err}")

    @staticmethod
    def normalize_holdings(raw_holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """KIS 잔고 output1을 code → position dict로 변환. qty=0인 항목은 제외."""
        result: dict[str, dict[str, Any]] = {}
        for h in raw_holdings:
            code = str(h.get("pdno", "")).strip()
            if not code:
                continue
            try:
                qty = int(h.get("hldg_qty") or 0)
            except (ValueError, TypeError):
                qty = 0
            if qty <= 0:
                continue
            result[code] = {
                "code": code,
                "name": str(h.get("prdt_name", "")),
                "qty": qty,
                "avg_price": float(h.get("pchs_avg_pric") or 0),
                "cur_price": float(h.get("prpr") or 0),
                "eval_amount": float(h.get("evlu_amt") or 0),
                "pnl": float(h.get("evlu_pfls_amt") or 0),
                "pnl_pct": float(h.get("evlu_pfls_rt") or 0),
            }
        return result

    def get_balance(self, max_retries: int = 3) -> dict[str, Any]:
        cfg = self.trade_cfg
        tr_id = "TTTC8434R" if cfg.is_live else "VTTC8434R"
        params = {
            "CANO": cfg.account_no,
            "ACNT_PRDT_CD": cfg.account_product_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        last_err = ""
        for attempt in range(1, max_retries + 1):
            self._throttle(cfg)
            resp = self._trade_client.get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                params=params,
                headers=self._headers(cfg, tr_id),
            )
            body: dict[str, Any] | None
            try:
                body = resp.json()
            except Exception:
                body = None

            if resp.status_code == 200 and body and body.get("rt_cd") == "0":
                return {
                    "holdings": body.get("output1", []),
                    "summary": (body.get("output2") or [{}])[0],
                }

            msg = (body or {}).get("msg1", resp.text[:200] if body is None else "")
            msg_cd = (body or {}).get("msg_cd", "") if body else ""
            last_err = f"status={resp.status_code} msg_cd={msg_cd} msg1={msg}"

            is_rate_limit = "초당" in (msg or "") or msg_cd in {"EGW00201", "EGW00121"}
            if (is_rate_limit or resp.status_code >= 500) and attempt < max_retries:
                wait = 0.5 * attempt
                log.warning("잔고 조회 재시도 %s, %.1fs 대기", last_err, wait)
                time.sleep(wait)
                continue
            break

        raise RuntimeError(f"KIS 잔고 조회 실패: {last_err}")

    def cancel_order(
        self,
        order_no: str,
        krx_fwdg_ord_orgno: str,
        qty: int,
    ) -> dict[str, Any]:
        """미체결 주문 취소. 주문 취소 엔드포인트 (order-rvsecncl, RVSE_CNCL_DVSN_CD=02).

        필요 파라미터:
          - order_no: 원주문번호 (ORGN_ODNO)
          - krx_fwdg_ord_orgno: 한국거래소 전송 주문조직번호 (ord_gno_brno)
          - qty: 취소 수량 (보통 미체결 잔량)

        반환: {order_no, raw}
        """
        cfg = self.trade_cfg
        if cfg.is_live:
            tr_id = "TTTC0803U"
        else:
            tr_id = "VTTC0803U"

        body = {
            "CANO": cfg.account_no,
            "ACNT_PRDT_CD": cfg.account_product_cd,
            "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "01",  # 시장가 원주문
            "RVSE_CNCL_DVSN_CD": "02",  # 02 = 취소
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",  # 잔량 전부 취소
        }

        hashkey = self.get_hashkey(body)
        self._throttle(cfg)
        resp = self._trade_client.post(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            json=body,
            headers={
                "authorization": f"Bearer {get_access_token(cfg)}",
                "appkey": cfg.app_key,
                "appsecret": cfg.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
                "hashkey": hashkey,
                "content-type": "application/json; charset=utf-8",
            },
        )
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(
                f"KIS 취소 응답 파싱 실패: status={resp.status_code} body={resp.text[:300]}"
            )

        if resp.status_code == 200 and data.get("rt_cd") == "0":
            output = data.get("output") or {}
            return {
                "order_no": str(output.get("ODNO", "")),
                "raw": data,
            }

        msg = data.get("msg1", "")
        msg_cd = data.get("msg_cd", "")
        raise RuntimeError(
            f"KIS 주문 취소 실패 ({order_no}): status={resp.status_code} "
            f"msg_cd={msg_cd} msg1={msg}"
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "KisClient":
        """Settings 객체에서 throttle 값을 뽑아 KisClient 를 생성.

        `settings.rate_limit` 블록을 읽어 live/paper 최소 간격을 적용.
        다섯 곳의 생성 호출을 한 곳에서 관리해 설정 변경 시 일관성 유지.
        """
        rl = getattr(settings, "rate_limit", {}) or {}
        return cls(
            trade_cfg=settings.kis,
            quote_cfg=settings.kis_quote,
            live_min_interval_sec=rl.get("live_min_interval_sec"),
            paper_min_interval_sec=rl.get("paper_min_interval_sec"),
        )

    @classmethod
    def from_settings_with_override(
        cls,
        settings: Any,
        trade_cfg: KisConfig,
    ) -> "KisClient":
        """런타임 모드/자격증명 교체 시 새 trade_cfg 로 KisClient 재생성.

        rate_limit 는 settings 에서 그대로, quote_cfg 도 settings.kis_quote 유지.
        """
        rl = getattr(settings, "rate_limit", {}) or {}
        return cls(
            trade_cfg=trade_cfg,
            quote_cfg=settings.kis_quote,
            live_min_interval_sec=rl.get("live_min_interval_sec"),
            paper_min_interval_sec=rl.get("paper_min_interval_sec"),
        )

    def inquire_daily_ccld(
        self,
        order_no: str | None = None,
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        """당일 주문 체결 조회 (inquire-daily-ccld).

        order_no 주면 해당 주문번호만 필터링해서 반환, None 이면 오늘 전체.
        반환: 주문별 체결 정보 리스트 (KIS output1).
          주요 필드:
            - odno: 주문번호
            - pdno: 종목코드
            - ord_qty: 주문 수량
            - tot_ccld_qty: 총 체결 수량
            - rmn_qty: 미체결 수량
            - cncl_yn: 취소 여부
            - ord_dvsn_name: 주문 구분 (시장가/지정가)
            - sll_buy_dvsn_cd: 매도/매수 구분
        """
        cfg = self.trade_cfg
        # 당일 체결: TTTC0081R (live) / VTTC0081R (paper)
        tr_id = "TTTC0081R" if cfg.is_live else "VTTC0081R"
        today = datetime.now().strftime("%Y%m%d")

        params = {
            "CANO": cfg.account_no,
            "ACNT_PRDT_CD": cfg.account_product_cd,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",  # 전체
            "INQR_DVSN": "00",         # 역순
            "PDNO": "",
            "CCLD_DVSN": "00",         # 전체
            "ORD_GNO_BRNO": "",
            "ODNO": order_no or "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        last_err = ""
        for attempt in range(1, max_retries + 1):
            self._throttle(cfg)
            resp = self._trade_client.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                params=params,
                headers=self._headers(cfg, tr_id),
            )
            body: dict[str, Any] | None
            try:
                body = resp.json()
            except Exception:
                body = None

            if resp.status_code == 200 and body and body.get("rt_cd") == "0":
                return list(body.get("output1") or [])

            msg = (body or {}).get("msg1", resp.text[:200] if body is None else "")
            msg_cd = (body or {}).get("msg_cd", "") if body else ""
            last_err = f"status={resp.status_code} msg_cd={msg_cd} msg1={msg}"

            is_rate_limit = "초당" in (msg or "") or msg_cd in {"EGW00201", "EGW00121"}
            if (is_rate_limit or resp.status_code >= 500) and attempt < max_retries:
                wait = 0.5 * attempt
                log.warning("체결 조회 재시도 %s, %.1fs 대기", last_err, wait)
                time.sleep(wait)
                continue
            break

        raise RuntimeError(f"KIS 체결 조회 실패: {last_err}")
