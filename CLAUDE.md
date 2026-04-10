# CLAUDE.md

이 파일은 Claude Code 가 이 저장소에서 작업할 때 참고하는 프로젝트 가이드입니다.

## 프로젝트 개요

한국투자증권(KIS) Open API 와 Anthropic Claude API 를 결합한 **국내주식 자동매매 봇**.
평일 09:00~15:30 KST 동안 10분 주기로 관심 종목을 점검하고, AI 판단과 리스크 매니저의
다단계 게이트를 거쳐 자동으로 시장가 주문을 실행한다. Synology NAS Docker 환경을 기본
상정하며 텔레그램으로 전 기능 원격 제어 가능.

## 기술 스택

- **Python 3.11+** (로컬 개발은 3.9 도 동작하지만 3.11 권장)
- httpx, pyyaml, python-dotenv, anthropic, apscheduler
- SQLite (상태/로그/시그널/주문 기록)
- Docker + GHCR + Watchtower (자동 배포 파이프라인)
- GitHub Actions (CI/CD)

## 개발 환경 세팅

```bash
# venv + 의존성
python3 -m venv .venv
source .venv/bin/activate
pip install "httpx[http2]>=0.27" "pyyaml>=6.0" "python-dotenv>=1.0" \
            "anthropic>=0.40" "apscheduler>=3.10"

# .env 작성 (README 의 '처음 시작하기' 섹션 참고)
cp .env.example .env
chmod 600 .env

# 단일 사이클 실행 (장외 시간에도 강제)
PYTHONPATH=. .venv/bin/python -m trading_bot.main --once --force

# 스케줄러 모드 (실제 운용처럼)
PYTHONPATH=. .venv/bin/python -m trading_bot.main

# 단계별 검증 스크립트
PYTHONPATH=. .venv/bin/python scripts/stage3_verify.py  # 주문 경로
PYTHONPATH=. .venv/bin/python scripts/stage4_verify.py  # 텔레그램 커맨드
PYTHONPATH=. .venv/bin/python scripts/stage6_verify.py  # 청산 전략

# 컴파일 체크 (모든 모듈 신택스 검증)
python3 -m compileall -q trading_bot/
```

## 아키텍처 요약

### 전체 점검 사이클 (`signals/cycle.py` run_cycle)

1. **잔고 조회** — KIS `inquire-balance` (현재 모드 서버)
2. **position_state 동기화** — 신규/청산된 포지션 반영, high_water_mark 갱신
3. **자동 청산 체크** (보유 포지션 각각)
   - 손절 (-5%), 익절 (+15%), 트레일링 스톱 (+7% 활성 / 고점 대비 -4%)
   - AI 판단 없이 기계적 규칙으로 즉시 시장가 판매
4. **유니버스 스캔** (새 진입 검토) — 종목당:
   - 일봉 OHLCV 조회 (실전 서버 강제)
   - RSI + 거래량 비율 계산
   - **1차 게이트**: 룰베이스 prefilter (RSI + 거래량)
   - **2차 게이트**: Claude LLM tool_use 구조화 판단 (decision + confidence + reasoning)
   - **3차 게이트**: confidence >= 0.75 임계값
   - **4차 게이트**: 리스크 매니저 7단계
5. **주문 실행** (시장가) → DB 기록 → Telegram 알림

### 리스크 매니저 7단계 (`risk/manager.py`)

1. side 검증 (buy/sell 만)
2. 킬 스위치 (구매만 차단, 판매는 허용)
3. 일일 주문 수 한도
4. 일일 손실 한도 (매수만)
5. 종목별 쿨다운
6. 중복 진입 차단
7. 동시 보유 종목 수 + 포지션 사이징

## 핵심 디자인 결정 (되돌리지 말 것)

- **시세 조회는 항상 실전 서버**
  모의 서버(openapivts)의 국내주식 시세 API 는 불안정해 (500 에러 빈발). `config.py` 의
  `kis_quote` 는 항상 `KIS_LIVE_*` 키로 초기화된다. 주문/잔고만 현재 모드 서버로.

- **paper 가 기본값**
  `.env.example`, `settings.yaml` 모두 `KIS_MODE=paper`. 실전 전환은 사용자가 명시적으로
  `.env` 수정 시에만. 테스트 코드에서도 paper 유지.

- **Telegram 만으로 운영 가능**
  SSH 접근 없이도 전 기능 조작 가능해야 함. kill switch 토글, 자동 업데이트 on/off,
  수동 판매, 사이클 강제 실행, 상태 조회 모두 텔레그램 커맨드로.

- **기계적 청산 우선**
  손절/익절/트레일링은 LLM 이 아닌 고정 규칙. 속도/신뢰성/비용 모두 유리.

- **`:latest` 이미지 태그는 tag push 에서만 갱신**
  main push 로 빌드된 이미지는 `:main` 과 `:sha-xxx` 만 받고 `:latest` 는 받지 않음.
  Watchtower 가 `:latest` 를 감시하므로 **의미 있는 릴리스(태그)만 NAS 에 자동 배포**됨.
  작은 수정은 main 에만 쌓고 태그는 묶어서.

## 언어·스타일 규칙

- **사용자 대면 텍스트는 한국어 쉬운 말**
  텔레그램 메시지, 에러 메시지, 로그는 전부 한국어. 토스 증권 스타일:
  - 매수/매도 → 구매/판매
  - 포지션 → 갖고 있는 주식
  - 평단가 → 평균 구매가
  - 예수금 → 쓸 수 있는 현금
  - confidence 0.75 → 확신도 75%
  - 금액에 천 단위 콤마 (`fmt_won` 헬퍼)

- **코드 주석/로그도 한국어**
  기술 용어는 원어 유지 (RSI, OHLCV, hashkey, Watchtower 등).

- **커밋 메시지**
  한국어 또는 한영 혼용. 제목 50자 이내. 본문에 "왜" 중심 설명. 마지막 줄에
  `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`.

## 릴리스 정책

- **모든 커밋을 태그하지 말 것**. 사소한 수정(오타, 로그 조정, 문구 다듬기, 주석
  개선 등)은 main 에 푸시만 하고 태그 생략.
- **의미 있는 변경이 누적됐을 때만 태그**:
  - 새 Stage 완성 / 주요 기능 추가
  - 새 텔레그램 커맨드
  - 사용자 워크플로우 변화
  - 버그 픽스 여러 개 묶어서
- **semver**: MAJOR.MINOR.PATCH
  - MAJOR: 호환성 깨지는 변경 (아직 없음)
  - MINOR: 새 기능 (Stage 추가 등)
  - PATCH: 버그 픽스, UX 개선, 작은 조정
- **태그 형식**: `v0.2.7`, `v0.3.0` 등

태그 찍는 법:
```bash
git tag -a v0.x.y -m "v0.x.y — 변경 요약"
git push origin v0.x.y
```

GitHub Actions 가 자동으로:
1. Docker 이미지 빌드 → GHCR 업로드 (`:latest`, `:0.x.y`, `:0.x`, `:sha-xxx`)
2. GitHub Release 페이지 생성 + changelog 자동 첨부

## KIS API 알려진 버그/주의 (절대 잊지 말 것)

1. **"초당 거래건수를 초과" 에러가 HTTP 500 + JSON 바디로 반환됨** (문서와 다름).
   `resp.raise_for_status()` 를 바디 확인 전에 호출하면 원인을 놓친다.
   → 바디 먼저 파싱하고 `msg1` 에서 "초당" 문자열 판정 → 0.5초 백오프 재시도.

2. **모의 서버 시세 API 불안정**
   같은 종목에도 500 랜덤 발생. 해결: 시세는 항상 실전 서버 + 실전 키 사용
   (`paper` 모드에서도). 이걸 되돌리면 모든 시세 조회가 불안정해짐.

3. **모의 서버 장외 시간 주문 거부**
   `msg_cd=40580000 msg1=모의투자 장종료 입니다`. 스케줄러가 평일 09:00~15:30 로
   제한돼 있어 자동 운용 중엔 문제 없음. `--once --force` 수동 테스트 시만 주의.

4. **보수적 rate limit 마진**
   - 실전 서버: 최소 0.12초 간격 (~8 req/s)
   - 모의 서버: 최소 0.55초 간격 (~1.8 req/s)
   이보다 빠르면 5종목 연속 조회에서도 500 튐.

5. **주문 hashkey 필수**
   POST `/uapi/domestic-stock/v1/trading/order-cash` 는 `hashkey` 헤더 필수.
   `/uapi/hashkey` 엔드포인트로 주문 body 의 hash 선발급. hashkey 호출 자체도
   throttle 대상 (이것도 rate limit 걸릴 수 있음).

6. **주문 재시도 정책**
   rate limit ("초당" 포함 msg1) 만 재시도 허용. 그 외 비즈니스 에러(잔고 부족,
   장종료, 상한가 도달 등) 는 **절대 재시도 금지** — 주문 중복 방지.

7. **모의계좌 초기 잔고**: 1억 원 (`dnca_tot_amt=100000000`).

8. **잔고 output2 의 `asst_icdc_erng_rt`** = 전일 대비 자산 증감률 %. 일일 손실
   한도 체크에 그대로 활용.

## 파일 구조 요점

```
trading_bot/
  main.py              엔트리포인트 (scheduler + telegram poller + setMyCommands)
  __init__.py          __version__ (BOT_VERSION env 우선, fallback 하드코딩)
  config.py            .env + settings.yaml 통합 로딩, KisConfig/Settings
  logging_setup.py     콘솔 + 파일 로거

  kis/
    auth.py            토큰 발급/캐시/자동 갱신 (lock 포함)
    client.py          REST 래퍼:
                         get_price (FHKST01010100, live 서버)
                         get_daily_ohlcv (FHKST03010100, live 서버)
                         get_balance (VTTC8434R/TTTC8434R, 모드별)
                         get_hashkey + place_market_order (VTTC0802U/TTTC0802U)
                         normalize_holdings (정적 헬퍼)
                         _throttle (서버별 최소 간격 + lock)

  signals/
    indicators.py      RSI (Wilder), volume_ratio, sma — 순수 Python
    prefilter.py       룰베이스 후보 선정 (Candidate dataclass)
    llm.py             Claude API + emit_decision tool_use
                         SYSTEM_PROMPT: 쉬운 말로 reasoning 작성 지시
    exit_strategy.py   check_exit (손절/익절/트레일링)
                         sync_position_state, update_high_water_mark
    cycle.py           run_cycle (전체 오케스트레이션)
                         _run_exit_checks (청산 단계)
                         _notify_summary (텔레그램 요약)

  risk/
    manager.py         RiskManager.check (7단계 게이트, is_exit 파라미터)
                         RiskDecision dataclass
    kill_switch.py     파일 기반 (data/KILL_SWITCH)
                         is_active, activate, deactivate

  bot/
    context.py         BotContext (settings, kis, risk, llm, trading_lock, started_at)
    commands.py        모든 텔레그램 커맨드 핸들러
                         TELEGRAM_BOT_COMMANDS (setMyCommands 용)
                         포맷 헬퍼: fmt_won, fmt_pct, decision_ko,
                                    mode_badge, confidence_pct, fmt_uptime
    poller.py          TelegramPoller (long polling 백그라운드 스레드)
                         chat_id 화이트리스트, 기동 시 backlog 스킵
    update_manager.py  자동 업데이트 토글 (data/AUTO_UPDATE_DISABLED)
                         GHCR digest 조회, GitHub Releases API
                         trigger_update (Watchtower HTTP API)

  store/
    db.py              SQLite 스키마 + 자동 마이그레이션
                         테이블: signals, orders, positions_snapshot,
                                 pnl_daily, errors, position_state
    repo.py            insert/query 헬퍼 함수들

  utils/
    calendar_kr.py     is_trading_day, is_market_open_now
                         KRX 휴장일 YAML 로드

  notify/
    telegram.py        sendMessage, getUpdates, answer_callback,
                         set_commands (setMyCommands)

config/
  settings.yaml            운용 파라미터 (universe, cycle_minutes, risk, llm,
                                          prefilter, exit)
  market_holidays.yaml     KRX 휴장일 (매년 갱신 필요)

scripts/
  stage3_verify.py         주문 경로 E2E 검증 (risk + hashkey + place_market_order)
  stage4_verify.py         텔레그램 커맨드 핸들러 단위 검증
  stage6_verify.py         exit_strategy.check_exit 시나리오 검증 (8개 케이스)

.github/workflows/
  docker-publish.yml       main push + v* tag push → GHCR 빌드/푸시
                             :latest 는 tag push 시에만 갱신 (중요!)
  release.yml              v* tag push → GitHub Release 자동 생성
```

## 테스트 / 검증 워크플로우

- **코드 수정 후**: `python3 -m compileall -q trading_bot/` (컴파일 체크 필수)
- **변경한 커맨드/로직**: 관련 `scripts/stageN_verify.py` 재실행
- **전체 사이클**: `python -m trading_bot.main --once --force`
- **NAS 배포**: `git push` 후 의미 있는 변경이면 `git tag v0.x.y && git push --tags`

## 자주 하는 실수 (회피)

- **`:latest` 를 단순 pull 하면 자동 배포된다고 생각하기**
  main push 는 `:latest` 를 갱신하지 않는다. 반드시 `git tag` 후 `git push --tags`.

- **Python 3.9 f-string 에서 백슬래시 사용**
  `f"{d[\"key\"]}"` 은 3.9 에서 SyntaxError. 로컬 venv 가 3.9 면 터진다.
  → 변수로 분리하거나 `f"{d['key']}"` (작은따옴표 혼용).

- **`.env` scp 전송 시 `-O` 옵션 누락**
  맥북 OpenSSH 9+ 는 SFTP 프로토콜 기본이지만 Synology DSM 은 SFTP 서브시스템
  꺼져있음. `scp -O -P <port> source dest` 사용. NAS SSH 접속 상태에서 직접
  `nano` 로 편집하는 게 더 단순할 때도 있음.

- **긴 URL 을 터미널에 붙여넣을 때 줄바꿈 됨**
  `sudo curl -o file URL` 에서 URL 이 잘려 두 줄로 해석되는 경우 있음.
  → 변수로 쪼개기: `URL=...; sudo curl -o file "$URL"`

- **마이그레이션 없이 DB 스키마 변경**
  기존 DB 와 호환되려면 `store/db.py` 의 `init_db` 에 `ALTER TABLE IF NOT EXISTS`
  패턴으로 마이그레이션 코드 추가.

- **주문 에러에 retry 를 무턱대고 달기**
  rate limit 만 재시도. 잔고 부족/장종료/상한가 등은 절대 재시도 금지.

## 개발 로드맵 (선택)

- Stage 7: 웹 대시보드 (FastAPI + 차트)
- Stage 8: Lean 엔진 백테스트 통합, 전략 튜닝
- 추가 아이디어: 뉴스 헤드라인 감성 분석, 펀더멘털 데이터 연동, 멀티 종목 가중치
  최적화, 실시간 웹소켓 시세 (현재는 REST 폴링)
