# CLAUDE.md

이 파일은 Claude Code 가 이 저장소에서 작업할 때 참고하는 프로젝트 가이드입니다.

## 프로젝트 개요

한국투자증권(KIS) Open API 와 Anthropic Claude API 를 결합한 **국내주식 자동매매 봇**.
평일 09:00~15:30 KST 동안 10분 주기로 관심 종목을 점검하고, AI 판단과 리스크 매니저의
다단계 게이트를 거쳐 자동으로 시장가 주문을 실행한다. Synology NAS Docker 환경을 기본
상정하며 **텔레그램 하나로 전 기능 원격 제어** 가능 (SSH 접근 없이도 운용 가능).

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

### APScheduler 크론 작업 4개 (`main.py`)

1. **cycle_job** — 평일 09:00~15:30 KST 매 `cycle_minutes` (기본 10분) 점검
2. **auto_update_job** — 매일 02:00 KST 자동 업데이트 체크 (Watchtower HTTP API 호출)
3. **paper_expiry_check_job** — 매일 08:00 KST 모의 계좌 90일 만료 체크
4. **credentials_watcher_job** — 5분마다 `data/credentials.env` mtime 감시, 변경 시 자동 재로드

## 런타임 상태 파일 (`data/` 볼륨)

컨테이너 재시작·이미지 업데이트에도 유지되는 영속 상태 파일들. 전부 `data/` 안에 있어
`docker-compose.yml` 볼륨 매핑(`./data:/app/data`)으로 자동 보존.

| 파일 | 용도 | 생성/수정 주체 |
|---|---|---|
| `trading.sqlite` | 시그널·주문·에러·포지션 기록 | `store/db.py`, `store/repo.py` |
| `KILL_SWITCH` | 긴급 정지 플래그 (파일 존재 시 구매 차단) | `risk/kill_switch.py`, `/stop` / `/resume` |
| `AUTO_UPDATE_DISABLED` | 자동 업데이트 꺼짐 플래그 | `bot/update_manager.py`, `/update disable` |
| `current_image_digest` | 기동 시 snapshot 한 GHCR digest (/update 비교용) | `bot/update_manager.py` |
| `kis_mode_override` | paper/live 모드 런타임 오버라이드 | `bot/mode_switch.py`, `/mode` |
| `credentials.env` | 자격증명 오버라이드 (`.env` 보다 우선) | `/setcreds`, `nano`, `credentials_watcher_job` |
| `paper_account_issued` | 모의 계좌 사용 시작 시각 (90일 만료 카운트다운) | `bot/expiry.py`, `/reload`, `/setcreds paper` |
| `universe.json` | 추적 종목 런타임 오버라이드 | `/universe add`, `/universe remove` |

## 텔레그램 커맨드 (17개)

**시작**
- `/menu`, `/start` — 메인 허브 (자주 쓰는 동작을 버튼 하나로)

**조회**
- `/help`, `/status`, `/positions`, `/signals`, `/cost`, `/mode`, `/universe`, `/about`

**조작**
- `/stop`, `/resume` — 킬 스위치 토글
- `/sell` — 보유 종목 목록을 버튼으로 띄워 선택 (또는 `/sell CODE` 로 직접 지정)
- `/positions` — 목록 + 종목별 판매 버튼
- `/cycle` — 사이클 1회 즉시 실행
- `/mode` — 현재 모드 표시 + 전환 버튼 (또는 `/mode paper|live [confirm]` 로 직접 전환)
- `/universe` — 목록
- `/universe add CODE` — 종목 추가 (KIS 이름 조회 후 예/아니오 버튼)
- `/universe remove` — 추적 종목 버튼 목록 → 선택 후 확인 (또는 `/universe remove CODE`)

**업데이트**
- `/update` — 현재/최신 버전 비교
- `/update confirm` — 실제 업데이트 실행 (이미 최신이면 한 줄 응답, Watchtower 호출 생략)
- `/update enable` / `disable` / `status` — 자동 업데이트 토글

**자격증명**
- `/setcreds paper KEY SECRET ACCOUNT` — 텔레그램 직접 교체 (원본 메시지 자동 삭제)
- `/setcreds live KEY SECRET ACCOUNT confirm` — 실전 교체 (confirm 필수)
- `/reload` — `data/credentials.env` 수동 재로드 (파일 없으면 `/setcreds` 안내)
- `/restart` — 컨테이너 완전 재시작 (`SIGTERM` → Docker restart 정책)

기동 시 `main.py` 가 `telegram.set_commands(TELEGRAM_BOT_COMMANDS)` 호출 → 텔레그램
`/` 자동완성 메뉴에 표시됨.

## 자격증명 관리 플로우 (3개월 모의 계좌 갱신)

KIS 모의투자 계좌는 **90일 유효기간** 이 있다. 재신청 시 새 앱키·시크릿·계좌번호가
발급되므로 정기적으로 교체 필요. 봇은 이걸 **Docker 재시작 없이** 처리한다.

**자동 경고**
- `bot/expiry.py` 가 `data/paper_account_issued` 파일에 저장된 시작 시각 + 90일 로
  만료일 계산
- 매일 08:00 KST 에 `paper_expiry_check_job` 이 체크
- 7일 이내: ⏰ "모의 계좌 만료 임박" 경고 (KIS 포털 링크 포함)
- 만료 후: 🚨 "모의 계좌 만료됨" + 경과일수 (사용자가 재신청할 때까지 매일)

**재발급 후 적용 방법 (3가지, 사용자 선택)**

**A. 텔레그램 `/setcreds` (가장 편함)**
```
/setcreds paper PSXXXxxx... longBase64String== 12345678
```
원본 메시지가 자동 삭제되어 시크릿이 채팅 기록에 남지 않는다.
로그에는 args 가 `[REDACTED]` 로 기록된다.

**B. 파일 편집 → 자동 감지 (5분 대기)**
```bash
nano /volume1/docker/trading/data/credentials.env
# 새 값 저장 후 최대 5분 내 credentials_watcher_job 이 감지, 자동 재로드
```

**C. 파일 편집 → 즉시 `/reload`**
```bash
nano data/credentials.env
# 저장 후 텔레그램: /reload
```

세 방법 모두 최종적으로 동일한 동작:
1. `data/credentials.env` 병합 저장 (다른 모드 키는 보존)
2. `load_credentials_override()` 로 `os.environ` 업데이트 (override=True)
3. `build_trade_cfg(current_mode)` 로 새 `KisConfig` 생성
4. `trading_lock` 안에서 `BotContext.kis` 원자적 교체
5. 해당 모드 토큰 캐시 (`tokens/kis_token_{mode}.json`) 삭제
6. paper 모드면 `expiry.mark_updated()` 호출 (90일 카운트다운 리셋)
7. `runtime_state.credentials_last_mtime` 갱신 (watcher 중복 방지)

## 핵심 디자인 결정 (되돌리지 말 것)

- **시세 조회는 항상 실전 서버**
  모의 서버(openapivts)의 국내주식 시세 API 는 불안정해 (500 에러 빈발). `config.py` 의
  `kis_quote` 는 항상 `KIS_LIVE_*` 키로 초기화된다. 주문/잔고만 현재 모드 서버로.

- **paper 가 기본값**
  `.env.example`, `settings.yaml` 모두 `KIS_MODE=paper`. 실전 전환은 사용자가 명시적으로
  `/mode live confirm` 또는 `.env` 수정 시에만. 테스트 코드에서도 paper 유지.

- **Telegram 만으로 운영 가능**
  SSH 접근 없이도 전 기능 조작 가능해야 함. kill switch 토글, 자동 업데이트 on/off,
  수동 판매, 사이클 강제 실행, 상태 조회, **자격증명 교체, 모드 전환, 컨테이너 재시작**
  모두 텔레그램 커맨드로.

- **기계적 청산 우선**
  손절/익절/트레일링은 LLM 이 아닌 고정 규칙. 속도/신뢰성/비용 모두 유리.

- **`:latest` 이미지 태그는 tag push 에서만 갱신**
  main push 로 빌드된 이미지는 `:main` 과 `:sha-xxx` 만 받고 `:latest` 는 받지 않음.
  Watchtower 가 `:latest` 를 감시하므로 **의미 있는 릴리스(태그)만 NAS 에 자동 배포**됨.
  작은 수정은 main 에만 쌓고 태그는 묶어서.

- **런타임 상태 파일은 전부 `data/` 안**
  `KILL_SWITCH`, `credentials.env`, `kis_mode_override`, `paper_account_issued`,
  `current_image_digest`, `AUTO_UPDATE_DISABLED` 전부. 이유: Docker 볼륨 매핑으로 영속화
  + 권한 분리 + 보안 (이미지에 포함 안 됨).

- **시크릿은 절대 로그/repo/이미지에 남기지 말 것**
  - `.env`, `data/credentials.env` 는 `.gitignore` 대상
  - poller 는 `/setcreds` 커맨드 args 를 `[REDACTED]` 로 마스킹
  - `/setcreds` 응답은 `delete_original=True` 플래그로 원본 메시지 자동 삭제
  - Docker 이미지는 `trading_bot/`, `config/`, `scripts/` 만 COPY (`.env` 불포함)

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
  기술 용어는 원어 유지 (RSI, OHLCV, hashkey, Watchtower, tool_use 등).

- **커밋 메시지**
  한국어 또는 한영 혼용. 제목 50자 이내. 본문에 "왜" 중심 설명. 마지막 줄에
  `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`.

## 릴리스 정책

- **모든 커밋을 태그하지 말 것**. 사소한 수정(오타, 로그 조정, 문구 다듬기, 주석
  개선 등)은 main 에 푸시만 하고 태그 생략. 사용자가 "축적 후 릴리스" 선호.
- **의미 있는 변경이 누적됐을 때만 태그**:
  - 새 Stage 완성 / 주요 기능 추가
  - 새 텔레그램 커맨드
  - 사용자 워크플로우 변화
  - 버그 픽스 여러 개 묶어서
- **semver**: MAJOR.MINOR.PATCH
  - MAJOR: 호환성 깨지는 변경 (아직 없음)
  - MINOR: 새 기능 (Stage 추가 등)
  - PATCH: 버그 픽스, UX 개선, 작은 조정
- **태그 형식**: `v0.2.8`, `v0.3.0` 등

태그 찍는 법:
```bash
git tag -a v0.x.y -m "v0.x.y — 변경 요약"
git push origin v0.x.y
```

GitHub Actions 가 자동으로:
1. Docker 이미지 빌드 → GHCR 업로드 (`:latest`, `:0.x.y`, `:0.x`, `:sha-xxx`)
2. GitHub Release 페이지 생성 + changelog 자동 첨부
3. 익일 02:00 KST Watchtower 자동 반영 (사용자 수동 액션 없음)

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

7. **모의계좌 초기 잔고는 신청 시점마다 다름**
   예전에는 1억원이었으나 최근 신청에서는 1천만원으로 확인됨 (2026-04). 유저에게
   `dnca_tot_amt` 가 1억이 아니어도 놀라지 않도록 안내.

8. **잔고 output2 의 `asst_icdc_erng_rt`** = 전일 대비 자산 증감률 %. 일일 손실
   한도 체크에 그대로 활용.

9. **90일 모의 계좌 만료**
   재신청 시 새 앱키/시크릿/계좌번호 전부 바뀜. 이걸 자동 감지할 수 없어서
   `data/paper_account_issued` 파일로 카운트다운 관리. `/reload` 또는 `/setcreds paper`
   시 리셋.

10. **토큰 발급 3개 동시 성공 가능**
    KIS OAuth `/oauth2/tokenP` 는 동일 앱키로 이미 발급된 토큰이 있으면 새 토큰이
    아니라 **같은 토큰** 을 돌려준다 (1일 유효). 여러 클라이언트(로컬 Mac, NAS)에서
    동시에 호출해도 같은 토큰이 나와서 무해.

## 주요 파일 구조

```
trading_bot/
  main.py              엔트리포인트
                         - setup_logging, load_settings, init_db
                         - snapshot_current_digest (기동 시 GHCR 비교 기준)
                         - expiry.ensure_issued_date (90일 카운트다운 초기화)
                         - telegram.set_commands (커맨드 메뉴 등록)
                         - TelegramPoller 스레드 시작
                         - APScheduler 4개 크론 잡 등록
                         - scheduler.start() (main thread block)
  __init__.py          __version__ (BOT_VERSION env 우선, fallback 하드코딩)
  config.py            .env + settings.yaml 통합 로딩
                         - ROOT, _MODE_OVERRIDE_FILE, CREDENTIALS_OVERRIDE_FILE
                         - KisConfig dataclass (mode, app_key/secret, account)
                         - Settings dataclass (kis, kis_quote, telegram, llm, ...)
                         - build_trade_cfg(mode) — public 헬퍼 (런타임 전환용)
                         - load_credentials_override() — data/credentials.env
                         - _read_mode_override() — data/kis_mode_override
                         - load_settings() — 전체 로드 + override 적용
  logging_setup.py     콘솔 + 파일 로거

  kis/
    auth.py            토큰 발급/캐시/자동 갱신 (lock 포함, 만료 1시간 전 재발급)
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
    context.py         BotContext dataclass
                         (settings, kis, risk, llm, trading_lock, started_at)
    commands.py        모든 텔레그램 커맨드 핸들러
                         TELEGRAM_BOT_COMMANDS (setMyCommands 용, 16개)
                         포맷 헬퍼: fmt_won, fmt_pct, decision_ko,
                                    mode_badge, confidence_pct, fmt_uptime
                         _reply(delete_original=False) — 응답 dict 빌더
    poller.py          TelegramPoller (long polling 백그라운드 스레드)
                         chat_id 화이트리스트, 기동 시 backlog 스킵
                         /setcreds args 는 로그 REDACTED
                         delete_original=True 응답 시 사용자 메시지 자동 삭제
    update_manager.py  자동 업데이트 토글 (data/AUTO_UPDATE_DISABLED)
                         GHCR digest 조회 + snapshot (data/current_image_digest)
                         GitHub Releases API
                         trigger_update (Watchtower HTTP API, ReadTimeout=성공)
    mode_switch.py     data/kis_mode_override 파일 read/write/clear
    expiry.py          PAPER_EXPIRY_DAYS=90
                         ensure_issued_date, mark_updated
                         days_until_expiry, build_expiry_warning
                         KIS_PORTAL_URL = apiportal.koreainvestment.com/intro
    runtime_state.py   모듈 간 공유 상태 (credentials_last_mtime)

  store/
    db.py              SQLite 스키마 + 자동 마이그레이션
                         테이블: signals, orders, positions_snapshot,
                                 pnl_daily, errors, position_state
                         orders 테이블 name/reason 컬럼 ALTER 추가 (마이그레이션)
                         signals.llm_cost_usd ALTER 추가 (마이그레이션)
    repo.py            insert/query 헬퍼 함수들

  utils/
    calendar_kr.py     is_trading_day, is_market_open_now
                         KRX 휴장일 YAML 로드

  notify/
    telegram.py        sendMessage, getUpdates, answer_callback
                         set_commands (setMyCommands)
                         delete_message (시크릿 메시지 자동 삭제)

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
                             BOT_VERSION build-arg 로 git tag 주입
  release.yml              v* tag push → GitHub Release 자동 생성 + changelog
```

## 테스트 / 검증 워크플로우

- **코드 수정 후**: `python3 -m compileall -q trading_bot/` (컴파일 체크 필수)
- **변경한 커맨드/로직**: 관련 `scripts/stageN_verify.py` 재실행
- **전체 사이클**: `python -m trading_bot.main --once --force`
- **NAS 배포**: `git push` 후 의미 있는 변경이면 `git tag v0.x.y && git push --tags`

## 자주 하는 실수 (회피)

- **`:latest` 를 단순 pull 하면 자동 배포된다고 생각하기**
  main push 는 `:latest` 를 갱신하지 않는다. 반드시 `git tag` 후 `git push --tags`.

- **`docker compose restart` 로 .env 변경 반영이 될 거라 기대**
  `restart` 는 env_file 을 **다시 읽지 않는다**. `.env` 변경 후엔 반드시
  `docker compose up -d --force-recreate trading-bot` 사용.

- **Python 3.9 f-string 에서 백슬래시 사용**
  `f"{d[\"key\"]}"` 은 3.9 에서 SyntaxError. 로컬 venv 가 3.9 면 터진다.
  → 변수로 분리하거나 `f"{d['key']}"` (작은따옴표 혼용).

- **`cut -d= -f2` 로 base64 시크릿 파싱**
  base64 패딩 `=` 이 있으면 `cut` 이 2번째 필드까지만 뽑아서 trailing `=` 을 버린다.
  → `sed -n 's/^KEY=//p'` 또는 `awk` 로 prefix 만 제거.

- **`.env` scp 전송 시 `-O` 옵션 누락**
  맥북 OpenSSH 9+ 는 SFTP 프로토콜 기본이지만 Synology DSM 은 SFTP 서브시스템
  꺼져있음. `scp -O -P <port> source dest` 사용. NAS SSH 접속 상태에서 직접
  `nano` 로 편집하는 게 더 단순할 때도 있음.

- **`scp -O` 를 NAS 셸에서 실행**
  `-O` 는 **Mac 의 OpenSSH 9+** 플래그. NAS 에는 없으니 맥북 터미널에서 실행해야 함.

- **Synology sudo 에 docker 경로 없음**
  `sudo docker` 가 "command not found" 에러. `secure_path` 가 `/usr/local/bin` 을
  포함 안 해서 발생. 해결: `sudo /usr/local/bin/docker ...` 절대 경로 사용.

- **ssh 로 sudo 명령 실행 시 TTY 없음**
  `sudo: a terminal is required`. 해결: `ssh -t` 플래그로 pseudo-TTY 할당.

- **긴 URL 을 터미널에 붙여넣을 때 줄바꿈 됨**
  `sudo curl -o file URL` 에서 URL 이 잘려 두 줄로 해석되는 경우 있음.
  → 변수로 쪼개기: `URL=...; sudo curl -o file "$URL"`

- **마이그레이션 없이 DB 스키마 변경**
  기존 DB 와 호환되려면 `store/db.py` 의 `init_db` 에 `ALTER TABLE ADD COLUMN IF NOT EXISTS`
  패턴 (또는 `PRAGMA table_info` 로 컬럼 존재 확인 후 ALTER) 으로 마이그레이션 코드 추가.

- **주문 에러에 retry 를 무턱대고 달기**
  rate limit 만 재시도. 잔고 부족/장종료/상한가 등은 절대 재시도 금지.

- **Watchtower HTTP API 의 긴 응답 지연**
  `trigger_update()` 는 Watchtower 가 이미지 pull + 컨테이너 교체 전부 완료 후에만
  응답. 이게 30~60초 걸릴 수 있으니 `httpx.ReadTimeout` 을 에러 아닌 **성공 (처리 중)**
  으로 해석해야 함. `update_manager.trigger_update` 에 이미 반영됨.

## 버전 히스토리 요약

| 버전 | 주요 내용 |
|---|---|
| v0.1.0 | Stage 1~5 완성 (골격, 시그널, 주문, 텔레그램 양방향, NAS 배포) |
| v0.1.1 | 토스 스타일 UI 전면 교체 |
| v0.2.0 | Stage 6 자동 청산 (손절/익절/트레일링) |
| v0.2.1 | 초보자 온보딩, /about, 버전 인젝션 |
| v0.2.2 | Watchtower 내부 cron 통합 |
| v0.2.3 | /update 커맨드 + Watchtower HTTP API 전환 |
| v0.2.4 | README TOC, 중복 제거, GPL v3 라이선스 |
| v0.2.5 | /update 의 digest 비교 "이미 최신" 응답 |
| v0.2.6 | Watchtower ReadTimeout 을 성공으로 처리 |
| v0.2.7 | /update 2단계 확인 플로우, :latest 는 tag push 전용 |
| v0.2.8 | **런타임 제어 대폭 강화**: /mode, /reload, /restart, /setcreds, 만료 자동 알림, credentials 자동 감시 |

## 개발 로드맵 (선택)

- Stage 7: 웹 대시보드 (FastAPI + 차트)
- Stage 8: Lean 엔진 백테스트 통합, 전략 튜닝
- 추가 아이디어: 뉴스 헤드라인 감성 분석, 펀더멘털 데이터 연동, 멀티 종목 가중치
  최적화, 실시간 웹소켓 시세 (현재는 REST 폴링)
