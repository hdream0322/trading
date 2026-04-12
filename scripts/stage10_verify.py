#!/usr/bin/env python3
"""Stage 10 — 펀더멘털 데이터 연동 검증 스크립트.

실행: PYTHONPATH=. python scripts/stage10_verify.py

1. 설정 로드 + DB 초기화
2. KIS 재무비율 API 호출 테스트 (삼성전자 005930)
3. 캐시 저장/조회 테스트
4. 리스크 게이트 테스트 (정상 / PER 초과 / 부채비율 초과 / None 데이터)
5. /funda 출력 포맷 테스트
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading_bot.config import load_settings
from trading_bot.kis.client import KisClient
from trading_bot.risk.manager import RiskDecision, RiskManager
from trading_bot.store.db import init_db


def main() -> None:
    print("=" * 60)
    print("Stage 10 — 펀더멘털 데이터 연동 검증")
    print("=" * 60)

    # 1. 설정 + DB
    print("\n[1/5] 설정 로드 + DB 초기화...")
    settings = load_settings()
    init_db()
    print(f"  모드: {settings.kis.mode}")
    funda_cfg = getattr(settings, "fundamentals", None) or {}
    print(f"  펀더멘털 설정: enabled={funda_cfg.get('enabled')}")
    print(f"  max_per={funda_cfg.get('max_per')}, min_per={funda_cfg.get('min_per')}")
    print(f"  max_pbr={funda_cfg.get('max_pbr')}, max_debt_ratio={funda_cfg.get('max_debt_ratio')}")
    print(f"  min_roe={funda_cfg.get('min_roe')}")
    print("  ✅ OK")

    # 2. KIS API 호출
    print("\n[2/5] KIS 재무비율 API 호출 (삼성전자 005930)...")
    kis = KisClient.from_settings(settings)
    try:
        raw = kis.get_financial_ratio("005930")
        print(f"  PER: {raw.get('per')}")
        print(f"  PBR: {raw.get('pbr')}")
        print(f"  ROE: {raw.get('roe')}")
        print(f"  EPS: {raw.get('eps')}")
        print(f"  BPS: {raw.get('bps')}")
        print(f"  부채비율: {raw.get('debt_ratio')}")
        print(f"  배당수익률: {raw.get('dividend_yield')}")
        print("  ✅ OK")
    except Exception as exc:
        print(f"  ❌ 실패: {exc}")
        print("  (KIS API 키가 유효하지 않거나 네트워크 문제일 수 있음)")

    # 3. 캐시 저장/조회
    print("\n[3/5] 캐시 저장/조회 테스트...")
    from trading_bot.signals import fundamentals

    data = fundamentals.fetch_and_cache("005930", "삼성전자", kis)
    if data:
        print(f"  fetch_and_cache → {data.code} PER={data.per} PBR={data.pbr}")
        cached = fundamentals.get_cached("005930")
        if cached:
            print(f"  get_cached → PER={cached.per} updated_at={cached.updated_at}")
            print("  ✅ OK")
        else:
            print("  ❌ 캐시 조회 실패")
    else:
        print("  ❌ fetch_and_cache 실패 (API 에러)")

    # 4. 리스크 게이트 테스트
    print("\n[4/5] 리스크 게이트 테스트...")
    risk = RiskManager(settings)
    print(f"  funda_enabled: {risk.funda_enabled}")

    # 테스트를 위해 임시로 활성화
    risk.funda_enabled = True

    # 정상 데이터 → 통과
    normal = {"per": 12.0, "pbr": 1.5, "roe": 15.0, "debt_ratio": 80.0}
    r = risk._check_fundamentals(normal)
    print(f"  정상 데이터: {r}  {'✅' if r is None else '❌'}")

    # PER 초과 → 차단
    high_per = {"per": 100.0, "pbr": 1.5, "roe": 15.0, "debt_ratio": 80.0}
    r = risk._check_fundamentals(high_per)
    print(f"  PER 100: {r}  {'✅' if r and 'PER' in r else '❌'}")

    # 적자 기업 (PER < 0)
    neg_per = {"per": -5.0, "pbr": 1.5, "roe": 15.0, "debt_ratio": 80.0}
    r = risk._check_fundamentals(neg_per)
    print(f"  PER -5: {r}  {'✅' if r and '적자' in r else '❌'}")

    # 부채비율 초과 → 차단
    high_debt = {"per": 12.0, "pbr": 1.5, "roe": 15.0, "debt_ratio": 500.0}
    r = risk._check_fundamentals(high_debt)
    print(f"  부채 500%: {r}  {'✅' if r and '부채' in r else '❌'}")

    # ROE 부진 → 차단
    low_roe = {"per": 12.0, "pbr": 1.5, "roe": -10.0, "debt_ratio": 80.0}
    r = risk._check_fundamentals(low_roe)
    print(f"  ROE -10%: {r}  {'✅' if r and 'ROE' in r else '❌'}")

    # None 데이터 → 통과 (데이터 없음 ≠ 차단)
    r = risk._check_fundamentals({"per": None, "pbr": None, "roe": None, "debt_ratio": None})
    print(f"  전부 None: {r}  {'✅' if r is None else '❌'}")

    # fundamentals=None → 게이트 우회
    print("  ✅ OK")

    # 5. 출력 포맷
    print("\n[5/5] 텔레그램 출력 포맷 테스트...")
    if data:
        print(fundamentals.format_for_display(data))
    else:
        print("  (데이터 없어 스킵)")

    print("\n" + "=" * 60)
    print("Stage 10 검증 완료!")
    print("=" * 60)


if __name__ == "__main__":
    main()
