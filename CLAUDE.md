# CLAUDE.md

이 파일은 Claude Code 가 이 저장소에서 작업할 때 참고하는 프로젝트 가이드입니다.
원칙: **코드/커밋/릴리스 노트에서 파악 가능한 내용은 여기 적지 않는다.** 여기에는
코드만 봐선 모르는 하드 정보(KIS API 버그, 되돌리지 말아야 할 디자인 결정, 사용자
선호)만 남긴다.

## 프로젝트 개요

한국투자증권(KIS) Open API + Anthropic Claude API 를 결합한 **국내주식 자동매매 봇**.
평일 09:00~15:30 KST 동안 10분 주기로 관심 종목을 점검하고, AI 판단과 리스크 매니저의
다단계 게이트를 거쳐 자동 시장가 주문. Synology NAS Docker 환경 + **텔레그램 하나로
전 기능 원격 제어** (SSH 없이도 운용 가능).

기술 스택: Python 3.11+, httpx, apscheduler, SQLite, Anthropic SDK, Docker + GHCR + Watchtower.

세부 아키텍처·파일 구조·커맨드 목록은 코드 자체와 `README.md` 를 참고. 전체 사이클은
`signals/cycle.py run_cycle`, 리스크 게이트는 `risk/manager.py RiskManager.check`,
스케줄러 잡은 `main.py` 에 모여 있다.

## 개발 환경 세팅

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "httpx[http2]>=0.27" "pyyaml>=6.0" "python-dotenv>=1.0" \
            "anthropic>=0.40" "apscheduler>=3.10,<4"
cp .env.example .env && chmod 600 .env

# 단일 사이클 강제 실행 (장외 시간에도)
PYTHONPATH=. .venv/bin/python -m trading_bot.main --once --force

# 스케줄러 모드
PYTHONPATH=. .venv/bin/python -m trading_bot.main

# 컴파일 체크 (코드 수정 후 필수)
python3 -m compileall -q trading_bot/

# 단계별 검증
PYTHONPATH=. .venv/bin/python scripts/stage3_verify.py  # 주문 경로
PYTHONPATH=. .venv/bin/python scripts/stage4_verify.py  # 텔레그램 커맨드
PYTHONPATH=. .venv/bin/python scripts/stage6_verify.py  # 청산 전략
PYTHONPATH=. .venv/bin/python scripts/stage10_verify.py # 펀더멘털 연동
```

## 런타임 상태 파일

컨테이너 재시작·이미지 업데이트에도 유지되어야 하는 상태는 **전부 `data/` 안**에 둔다
(`docker-compose.yml` 볼륨 매핑으로 영속화, 이미지에 포함 안 됨, 권한 분리).

`trading.sqlite`, `KILL_SWITCH`, `KILL_SWITCH_AUTO_RELEASE.log`, `AUTO_UPDATE_DISABLED`,
`QUIET_MODE`, `FUNDA_ENABLED`, `current_image_digest`, `kis_mode_override`,
`credentials.env`, `paper_account_issued`, `universe.json`, `init_notice_sent`,
`backup/trading_YYYYMMDD.sqlite`.

신규 상태 파일을 추가할 때는 **반드시 `data/` 하위**로. `.env` 과 `data/credentials.env`
는 `.gitignore` 대상, Docker 이미지 빌드 시 COPY 대상에서 제외.

## 핵심 디자인 결정 (되돌리지 말 것)

- **시세 조회는 항상 실전 서버.** 모의 서버(openapivts) 국내주식 시세 API 는 불안정
  (500 랜덤). `config.py kis_quote` 는 항상 `KIS_LIVE_*` 키로 초기화. 주문/잔고만
  현재 모드 서버. 이걸 되돌리면 모든 시세 조회가 불안정해짐.

- **paper 가 기본값.** `.env.example`, `settings.yaml`, 테스트 코드 전부 `KIS_MODE=paper`.
  실전 전환은 사용자가 명시적으로 `/mode live confirm` 또는 `.env` 수정할 때만.

- **Telegram 만으로 운영 가능.** SSH 없이도 전 기능 조작 가능해야 함 (킬 스위치, 자동
  업데이트, 수동 판매, 사이클 강제 실행, 자격증명 교체, 모드 전환, 컨테이너 재시작).
  새 운영 기능 추가 시 반드시 텔레그램 커맨드로 노출.

- **기계적 청산 우선.** 손절/익절/트레일링은 LLM 이 아닌 고정 규칙. 속도/신뢰성/비용
  모두 유리. LLM 은 **신규 진입 판단만**.

- **사일런트 실패 방지 — 에러 급증 자동 회로차단기 + 자동 복구.** 5분마다 최근 1시간
  에러 10건 초과 시 킬스위치 자동 활성화. 자동 활성화된 킬스위치(`trigger: auto`)는
  15분 경과 + 최근 30분 에러 0건일 때 자동 해제, 1시간 내 재활성화되면 플래핑으로
  판정해 수동만 가능. **수동 킬스위치는 절대 자동 해제 안 함** (구조적 문제일 수 있음).

- **submitted ≠ filled.** 시장가 주문도 상한가·거래정지·호가 부족으로 미체결 가능.
  사이클 끝에 `fill_tracker.reconcile_pending_orders` 가 KIS inquire-daily-ccld 로
  상태 업데이트 (filled/partial/cancelled). 미체결 매수는 자동 취소, 미체결 판매는
  계속 대기 (손절/청산이라 강제 취소 안 함).

- **LLM 프롬프트 캐싱 (ephemeral).** `signals/llm.py` 의 system 프롬프트 + tool 정의에
  `cache_control` 태그. 연속 호출 시 입력 비용 최대 90% 절감. 최소 캐시 블록(1024 토큰)
  미달 시 정상 과금으로 조용히 폴백.

- **LLM side_hint 미전달.** prefilter 가 buy/sell 방향을 결정해도 LLM 에는 알리지 않음
  (확증 편향 제거). 대신 prefilter side 와 LLM decision 교차검증해서 불일치 시 reject.

- **`/quiet` 는 10분 사이클 hold-only 요약만 토글.** 거래/청산/차단/에러/브리핑은 quiet
  여부와 무관하게 항상 전송. 장 시작/마감 브리핑도 quiet 와 독립이라 매일 평일
  09:00/15:35 무조건 전송.

- **`:latest` 이미지 태그는 tag push 에서만 갱신.** main push 는 `:main`, `:sha-xxx`
  만 받고 `:latest` 미갱신. Watchtower 가 `:latest` 감시하므로 **의미 있는 릴리스(태그)만
  NAS 자동 배포**됨. 작은 수정은 main 에 쌓고 태그는 묶어서.

- **시크릿은 절대 로그/repo/이미지에 남기지 말 것.** `.env`, `data/credentials.env`
  는 `.gitignore`. poller 는 `/setcreds` args 를 `[REDACTED]` 마스킹. `/setcreds`
  응답은 `delete_original=True` 로 원본 메시지 자동 삭제.

## KIS API 알려진 버그/주의 (절대 잊지 말 것)

코드만 봐선 모르는 운영 지식. 이걸 놓치면 디버깅이 몇 시간 이상 걸린다.

1. **"초당 거래건수를 초과" 에러가 HTTP 500 + JSON 바디로 반환됨** (문서와 다름).
   `resp.raise_for_status()` 를 바디 확인 전에 호출하면 원인을 놓친다. 바디 먼저
   파싱하고 `msg1` 에서 "초당" 문자열 판정 → 0.5초 백오프 재시도.

2. **모의 서버 시세 API 불안정.** 같은 종목에도 500 랜덤 발생. 시세는 항상 실전 서버
   + 실전 키 사용 (paper 모드에서도).

3. **모의 서버 장외 시간 주문 거부.** `msg_cd=40580000 msg1=모의투자 장종료 입니다`.
   스케줄러가 평일 09:00~15:30 로 제한돼 있어 자동 운용 중엔 문제없음. `--once --force`
   수동 테스트 시만 주의.

4. **KIS 유량 정책 (v0.5.0 기준).** 실전 20 req/s · 모의 2 req/s. **신규 API 발급 후
   3일**은 실전도 3 req/s 로 임시 제한. `settings.yaml rate_limit`:
   - 실전: `live_min_interval_sec: 0.055` (~18 req/s, 한도 90%)
   - 모의: `paper_min_interval_sec: 0.55` (~1.8 req/s, 한도 90%)
   - **신규 키 3일간 임시**: `live_min_interval_sec` 를 `0.34` (~2.9 req/s) 로 수정
     했다가 3일 경과 후 `0.055` 로 복원.
   `KisClient._throttle()` 이 서버별 최소 간격 강제 (multithread lock). hashkey 발급도
   같은 throttle 대상.

5. **주문 hashkey 필수.** `/uapi/domestic-stock/v1/trading/order-cash` POST 는 `hashkey`
   헤더 필수. `/uapi/hashkey` 로 선발급. hashkey 호출 자체도 throttle 대상.

6. **주문 재시도 정책.** rate limit ("초당" 포함 msg1) 만 재시도. 잔고 부족/장종료/
   상한가 등 비즈니스 에러는 **절대 재시도 금지** (주문 중복 방지).

7. **모의 계좌 초기 잔고는 신청 시점마다 다름.** 예전엔 1억원, 최근엔 1천만원 (2026-04).
   `dnca_tot_amt` 가 1억이 아니어도 놀라지 말 것.

8. **잔고 output2 의 `asst_icdc_erng_rt`** = 전일 대비 자산 증감률 %. 일일 손실 한도
   체크에 그대로 활용.

9. **90일 모의 계좌 만료.** 재신청 시 앱키/시크릿/계좌번호 전부 바뀜. 자동 감지 불가.
   `data/paper_account_issued` 로 카운트다운. `/reload` 또는 `/setcreds paper` 시 리셋.
   `paper_expiry_check_job` 이 매일 08:00 체크, 7일 이내부터 경고.

10. **토큰 재사용.** KIS OAuth `/oauth2/tokenP` 는 동일 앱키에 유효 토큰이 있으면
    **같은 토큰** 을 돌려줌 (1일 유효). 여러 클라이언트 동시 호출해도 무해.

## 자주 하는 실수 (프로젝트 고유)

- **`docker compose restart` 로 `.env` 변경이 반영될 거라 기대.** `restart` 는 env_file
  을 다시 읽지 않는다. `docker compose up -d --force-recreate trading-bot` 사용.

- **마이그레이션 없이 DB 스키마 변경.** 기존 DB 호환을 위해 `store/db.py init_db` 에
  `PRAGMA table_info` → `ALTER TABLE ADD COLUMN` 패턴으로 마이그레이션 코드 추가.

- **주문 에러에 retry 를 무턱대고 달기.** rate limit 만 재시도. 그 외는 금지.

- **Watchtower HTTP API 의 긴 응답 지연.** `trigger_update()` 는 이미지 pull + 컨테이너
  교체 완료 후에만 응답 (30~60초). `httpx.ReadTimeout` 을 **성공 (처리 중)** 으로
  해석해야 함. `update_manager.trigger_update` 에 이미 반영됨.

- **LLM 모델 교체 시 단가 동기화 누락.** Anthropic API 응답은 **토큰 수만** 주고 비용은
  안 준다. 봇이 `signals/llm.py` 에서 `settings.yaml llm.input_price_per_mtok` /
  `output_price_per_mtok` 로 직접 곱해 SQLite `signals.llm_cost_usd` 에 적재 →
  `repo.today_llm_cost_usd()` 가 합산해 `daily_cost_limit_usd` 게이트와 `/cost` /
  브리핑에 노출. **`llm.model` 만 바꾸고 단가를 안 바꾸면** 누적 비용이 실제와 어긋나
  한도가 헐거워지거나 빡세짐. 모델 교체 PR 에는 반드시 `input_price_per_mtok` /
  `output_price_per_mtok` 동시 수정. 캐시 1024 토큰 미달 폴백도 모델별로 다를 수 있어
  `llm.py` 캐시 주석도 함께 점검.

## 언어·스타일 규칙

- **사용자 대면 텍스트는 한국어 쉬운 말** (토스 증권 스타일): 매수/매도 → 구매/판매,
  포지션 → 갖고 있는 주식, 평단가 → 평균 구매가, 예수금 → 쓸 수 있는 현금, confidence
  0.75 → 확신도 75%. 금액에 천 단위 콤마 (`fmt_won` 헬퍼).

- **코드 주석/로그도 한국어.** 기술 용어는 원어 유지 (RSI, OHLCV, hashkey, Watchtower,
  tool_use 등).

- **커밋 메시지.** 한국어 또는 한영 혼용. 제목 50자 이내. 본문에 "왜" 중심 설명.
  마지막 줄에 `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`.

## 릴리스 정책

- **모든 커밋을 태그하지 말 것.** 사소한 수정(오타, 로그/문구/주석 조정)은 main push
  만. 사용자가 "축적 후 릴리스" 선호.
- **의미 있는 변경이 누적됐을 때만 태그**: 새 Stage / 새 텔레그램 커맨드 / 워크플로우
  변화 / 버그 픽스 묶음.
- **semver**: MAJOR (호환성 깨짐, 아직 없음) / MINOR (새 기능) / PATCH (버그·UX).
- **태그 형식**: `v0.x.y`. `git tag -a v0.x.y -m "..." && git push origin v0.x.y`.
  GitHub Actions 가 GHCR 빌드 + `:latest` 갱신 + Release 페이지 + changelog 자동 처리.
  익일 08:30 KST Watchtower 가 NAS 자동 반영.

## 테스트 / 검증 워크플로우

- **코드 수정 후**: `python3 -m compileall -q trading_bot/` 필수
- **변경한 커맨드/로직**: 관련 `scripts/stageN_verify.py` 재실행
- **전체 사이클**: `python -m trading_bot.main --once --force`
- **NAS 배포**: `git push` 후 의미 있는 변경이면 `git tag v0.x.y && git push --tags`

## 개발 로드맵 (선택)

- Stage 7: 웹 대시보드 (FastAPI + 차트)
- Stage 8: Lean 엔진 백테스트 통합, 전략 튜닝
- Stage 9: **뉴스 데이터 연동** (헤드라인 감성 분석)
- Stage 10: **펀더멘털 데이터 연동** (재무지표 리스크 게이트)
- 기타: 멀티 종목 가중치 최적화, 실시간 웹소켓 시세 (현재 REST 폴링)

### Stage 9 — 뉴스 데이터 연동 (계획)

**목표**: 종목별 최신 헤드라인을 LLM 판단 입력에 포함. 급등/급락 뉴스 직후 반대
방향 진입 차단, 재료 없는 기술적 반등/하락 필터링.

**데이터 소스**: 네이버 금융 뉴스 크롤링 (`finance.naver.com/item/news.naver?code=...`)
1순위, KIS 국내뉴스 API 있으면 2순위. 해외 API (NewsAPI, Finnhub) 는 한국 커버리지
약해서 제외.

**아키텍처**
- 새 모듈 `signals/news.py`: `fetch_news(code, limit=5, max_age_hours=24)` → 24h TTL 캐시
- 새 테이블 `news_cache` (중복 방지 + 과거 뉴스-가격 상관 분석용)
- `signals/llm.py` SYSTEM_PROMPT 에 `news: [{title, hours_ago}]` 블록 추가
- `signals/cycle.py` prefilter 통과 후 LLM 호출 전 `fetch_news` 호출
- 디버깅용 `/news CODE` 텔레그램 커맨드

**원칙**: 보조 입력 — 연동 실패 시 기존 로직대로 진행. 크롤링 차단 리스크는
robots.txt 준수 + User-Agent 설정으로 완화. 한국어 감성 분석은 별도 모델 대신
LLM 원문 전달 (더 정확).

### Stage 10 — 펀더멘털 데이터 연동 (계획)

**목표**: 재무지표 기반 리스크 게이트. 기술적 시그널이 떠도 PER/PBR 비정상, 부채비율
과도한 종목은 구조적으로 차단. 가치 + 모멘텀 하이브리드.

**데이터 소스**: KIS 재무비율 / 대차대조표 / 손익계산서 API (1순위, 인증 공유).
DART Open API (2순위, 원본 공시 XML). Yahoo/네이버 크롤링 제외.

**아키텍처**
- 새 모듈 `signals/fundamentals.py`: `fetch_fundamentals(code)` → PER/PBR/ROE/부채비율/EPS
  성장/배당수익률
- 새 테이블 `fundamentals_cache` + 주 1회 갱신 (분기 발표 주기 고려)
- 새 크론 잡 `fundamentals_refresh_job`: 매주 일요일 03:00 KST 유니버스 전체 갱신
  (KIS throttle 고려해 장외 시간 배치)
- `risk/manager.py` 에 9단계 게이트 추가 (섹터 게이트 다음):
  - `settings.yaml fundamentals`: `max_per`, `max_pbr`, `max_debt_ratio`, `min_roe`
  - **임계값 위반 시 매수만 차단, 매도는 허용** (이미 보유한 부실 종목은 빠져나와야 함)
  - **데이터 없음(캐시 미스) → 차단하지 않음** (연동 장애로 매수 막히는 상황 방지)
- `signals/llm.py` 입력에 펀더멘털 블록 추가
- 디버깅용 `/funda CODE` 텔레그램 커맨드

**원칙**: 성장주 배제 위험 완화 위해 `max_per` 넉넉하게 기본값. 가치 함정은 기존
추세 필터 (`현재가 > SMA20`, v0.5.0) 로 커버.

### Stage 9, 10 공통 원칙

- **보조 입력 원칙** — 연동 실패/데이터 없음 시 사이클이 멈추면 안 됨. fallback 은
  "해당 입력 없이 기존 로직대로 진행".
- **설정 기반 활성화** — `settings.yaml news.enabled`, `fundamentals.enabled` 기본 `false`
  로 시작, 로컬 검증 후 활성화.
- **사후 검증 필수** — 도입 후 최소 2주간 `/accuracy` 로 전/후 적중률 비교. 개선 없으면
  플래그 끄고 롤백.
