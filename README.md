# KIS Auto Trading Bot

한국투자증권(KIS) Open API와 Anthropic Claude API를 결합한 국내주식 자동매매 봇입니다.
룰베이스 사전필터 + LLM 판단 + 리스크 매니저의 3중 게이트 구조로 동작하며, 텔레그램 양방향
제어와 Synology NAS Docker 배포를 기본 상정합니다.

> ⚠️ 이 봇은 실제 돈을 움직일 수 있습니다. 반드시 모의투자(`KIS_MODE=paper`)로 최소 4주간
> 검증한 뒤 실전 전환 여부를 판단하세요. 제작자는 어떤 손실에도 책임지지 않습니다.

## 주요 기능

- **3중 안전장치**: 룰베이스 사전필터 → Claude LLM 판단(confidence 임계값) → 7단계 리스크 매니저
- **이원화된 서버 경로**: 시세 조회는 실전 서버, 주문/잔고는 현재 모드(paper/live) 서버
- **KIS rate limit 자동 재시도**: 초당 거래 건수 초과 에러 백오프 재시도
- **일일 비용 한도**: Claude API 호출 비용이 일일 한도 도달 시 자동 차단
- **킬 스위치**: 파일 기반 또는 텔레그램 `/stop` 커맨드로 신규 매수 즉시 차단
- **텔레그램 양방향 제어**: 11개 커맨드 + 인라인 버튼, chat_id 화이트리스트
- **한국 휴장일 캘린더**: KRX 휴장일 YAML로 관리, 주말/휴장일 자동 스킵
- **Docker 기반 배포**: Synology Container Manager 바로 가동

## 아키텍처

```
┌─ APScheduler (평일 09:00~15:30 KST, 10분 주기) ─┐
│                                                  │
│  유니버스 순회:                                  │
│    ├─ KIS 일봉 OHLCV 수집 (실전 서버)            │
│    ├─ RSI(14) + 거래량 비율 계산                 │
│    ├─ 1차 게이트: 룰베이스 prefilter             │
│    │   (RSI 임계치 + 거래량 ≥ min_volume_ratio)  │
│    ├─ 2차 게이트: Claude API (tool_use 구조화)   │
│    │   (decision + confidence + reasoning)       │
│    ├─ confidence ≥ threshold 통과 시만 다음 단계 │
│    ├─ 3차 게이트: 리스크 매니저 7단계            │
│    │   • 킬 스위치                               │
│    │   • 일일 주문 수 한도                       │
│    │   • 일일 손실 한도                          │
│    │   • 종목별 쿨다운                           │
│    │   • 중복 진입 차단                          │
│    │   • 동시 보유 한도                          │
│    │   • 포지션 사이징                           │
│    └─ KIS 시장가 주문 → DB 로그 → 텔레그램 알림  │
│                                                  │
└──────────────────────────────────────────────────┘
       ▲                              ▼
       │                    [SQLite trading.sqlite]
┌──────┴────────────┐          signals / orders /
│ Telegram Poller   │          errors / pnl_daily
│ (long polling)    │
│ 11 커맨드 + 버튼  │
└───────────────────┘
```

## 요구사항

- **한국투자증권 Open API 계정** (모의투자 앱키/시크릿 + 계좌번호)
- **Anthropic API 키** ([console.anthropic.com](https://console.anthropic.com))
- **텔레그램 봇** (BotFather로 생성, chat_id 확보)
- **로컬 개발 환경**: Python 3.11+ (3.9+에서도 동작하지만 3.11 권장)
- **배포 환경**: Docker 가능한 리눅스 머신 (Synology NAS DS423+, DSM 7.2+ 기준 검증)

## 빠른 시작 (로컬)

```bash
git clone https://github.com/hdream0322/trading.git
cd trading

# 시크릿 파일 생성
cp .env.example .env
chmod 600 .env
# 에디터로 .env 열고 아래 키 채우기:
#   KIS_MODE=paper
#   KIS_PAPER_APP_KEY=...
#   KIS_PAPER_APP_SECRET=...
#   KIS_PAPER_ACCOUNT_NO=...
#   (실전 키는 KIS_LIVE_* 접두사로 별도 관리 가능)
#   ANTHROPIC_API_KEY=...
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...

# 의존성 설치
python3 -m venv .venv
source .venv/bin/activate
pip install "httpx[http2]>=0.27" "pyyaml>=6.0" "python-dotenv>=1.0" \
            "anthropic>=0.40" "apscheduler>=3.10"

# Stage 1 smoke test (토큰 → 잔고 → 5종목 시세 → 텔레그램)
python -m trading_bot.smoke_test

# 단일 사이클 수동 실행 (장외 시간도 강제 실행)
python -m trading_bot.main --once --force

# 스케줄러 모드 (평일 09:00~15:30 KST 자동)
python -m trading_bot.main
```

## Docker 배포 (Synology NAS)

### 사전 준비

1. DSM → **패키지 센터**에서 **Container Manager** 설치
2. DSM → **제어판 → 터미널 및 SNMP**에서 SSH 서비스 활성화
3. File Station에서 `/volume1/docker/` 폴더 존재 확인 (Container Manager 설치 시 자동 생성)

### 배포 단계

```bash
# 1) NAS로 SSH 접속
ssh <your-account>@<nas-ip>

# 2) git clone
cd /volume1/docker
git clone https://github.com/hdream0322/trading.git
cd trading

# 3) .env 파일 생성
# 맥북 등 로컬에서 별도로 작성해둔 .env 파일을 scp로 전송하거나,
# NAS에서 vi/nano로 직접 생성:
nano .env
chmod 600 .env

# 4) 빌드 및 기동
sudo docker compose up -d --build

# 5) 로그 확인
sudo docker compose logs -f trading-bot
```

기동 성공 시 텔레그램으로 `*봇 기동* 🟡 PAPER — 사이클 10분 주기` 메시지가 도착합니다.

### 업데이트

```bash
cd /volume1/docker/trading
git pull
sudo docker compose up -d --build
```

### 중지 / 재시작

```bash
sudo docker compose stop           # 임시 중지
sudo docker compose start          # 재시작
sudo docker compose down           # 완전 종료 (볼륨은 유지)
sudo docker compose restart        # 컨테이너 재시작
```

### Container Manager GUI에서 관리

Container Manager → Container 탭 → `trading-bot` 선택 → 로그, 재시작, 중지 버튼 사용.
프로젝트는 **Project** 탭에서 docker-compose.yml 기반으로 관리됩니다.

## 설정

### `.env` — 시크릿

```bash
KIS_MODE=paper                    # paper | live
KIS_PAPER_APP_KEY=...
KIS_PAPER_APP_SECRET=...
KIS_PAPER_ACCOUNT_NO=...          # 8자리 계좌번호
KIS_PAPER_ACCOUNT_PRODUCT_CD=01
KIS_LIVE_APP_KEY=...              # (선택) 실전 키 - 모드 토글용
KIS_LIVE_APP_SECRET=...
KIS_LIVE_ACCOUNT_NO=...

ANTHROPIC_API_KEY=...             # Claude API
TELEGRAM_BOT_TOKEN=...             # BotFather에서 발급
TELEGRAM_CHAT_ID=...               # 본인 채팅 ID

TZ=Asia/Seoul
LOG_LEVEL=INFO
```

**중요**: `.env`는 `.gitignore`에 포함되어 있으며 **절대 커밋하지 마세요**. 파일 권한은
`600`으로 제한합니다.

### `config/settings.yaml` — 운용 파라미터

```yaml
mode: paper  # KIS_MODE 환경변수가 우선

universe:
  - {code: "005930", name: "삼성전자"}
  - {code: "000660", name: "SK하이닉스"}
  - {code: "035720", name: "카카오"}
  - {code: "035420", name: "NAVER"}
  - {code: "005380", name: "현대차"}

cycle_minutes: 10                    # 사이클 주기
market_hours:
  open: "09:00"
  close: "15:30"

risk:
  max_position_per_symbol_pct: 15    # 종목당 총자산 비중 상한 (%)
  max_concurrent_positions: 3        # 동시 보유 최대 종목 수
  daily_loss_limit_pct: 3            # 전일 대비 -X% 손실 시 신규 매수 차단
  cooldown_minutes: 60               # 같은 종목 재진입 대기 시간
  max_orders_per_day: 6              # 일일 총 주문 건수 상한

llm:
  model: "claude-haiku-4-5-20251001"
  temperature: 0
  confidence_threshold: 0.75         # 시그널 발효 임계값
  daily_cost_limit_usd: 5            # 일일 Claude 비용 한도
  input_price_per_mtok: 1.0
  output_price_per_mtok: 5.0

prefilter:
  rsi_period: 14
  rsi_buy_below: 35
  rsi_sell_above: 70
  min_volume_ratio: 1.2              # 20일 평균 대비 거래량 최소 배수
```

### `config/market_holidays.yaml` — 휴장일

한국거래소(KRX) 공식 달력을 매년 갱신. 양식은 파일 주석 참고.

## 텔레그램 커맨드

| 커맨드 | 기능 |
|---|---|
| `/help` | 커맨드 목록 |
| `/status` | 모드, 총자산, 킬스위치, LLM 비용 + 퀵 액션 버튼 |
| `/mode` | 현재 거래 모드와 계좌 |
| `/universe` | 추적 종목 목록 |
| `/positions` | 보유 포지션 상세 (수량, 평단, 현재가, 평가손익) |
| `/signals` | 오늘 발생 시그널 최근 10건 |
| `/cost` | 오늘 LLM 누적 비용 / 한도 대비 % |
| `/stop` / `/kill` | 🛑 킬 스위치 활성 (신규 매수 차단) |
| `/resume` | ✅ 킬 스위치 해제 |
| `/sell <종목코드>` | 특정 종목 전량 매도 (확정 버튼 필요) |
| `/cycle` | 사이클 1회 즉시 강제 실행 |

사이클 요약 메시지에는 자동으로 `[🛑 긴급 정지] [✅ 해제] [📊 포지션] [💰 상태]` 인라인 버튼이
첨부되어 한 탭으로 조작 가능합니다.

## 안전장치

### 1. 모드 분리 (paper / live)

`.env`의 `KIS_MODE=paper` 가 기본값입니다. 실전 전환은 최소 4주 모의투자 검증 후에만
권장합니다. paper와 live 계좌 키를 `.env`에 **동시에** 저장해두고 `KIS_MODE` 한 줄만 바꿔
전환하는 구조라, 키 교체 중 실수로 실전에 쏘는 사고를 예방합니다.

### 2. 3중 판단 게이트

1. **룰베이스 prefilter**: RSI 임계치와 거래량 배수 조건을 동시에 만족해야 후보 선정.
   중립 구간 종목은 LLM 호출 없이 즉시 hold → 비용 절약.
2. **Claude LLM**: `temperature=0` + `tool_use` 구조화 출력으로 `decision/confidence/reasoning`
   강제. 확신이 낮으면 스스로 hold + 낮은 confidence 반환.
3. **Confidence 임계값**: `confidence_threshold` (기본 0.75) 미만은 DB 기록만 되고 주문
   실행 안 함.

### 3. 7단계 리스크 매니저

모든 주문은 `RiskManager.check()`를 통과해야 실행됩니다:

1. side 검증 (buy/sell만)
2. 킬 스위치 (매수 차단, 매도 허용)
3. 일일 주문 수 한도
4. 일일 손실 한도 (매수만)
5. 종목별 쿨다운
6. 중복 진입 차단 (이미 보유 중이면 매수 금지, 미보유면 매도 금지)
7. 동시 보유 종목 수 + 포지션 사이징

### 4. 킬 스위치

파일 기반(`data/KILL_SWITCH`)과 텔레그램 커맨드(`/stop` `/resume`) 두 경로로 조작 가능.
**매수만 차단되고 매도는 계속 허용**되어, 활성 상태에서도 기존 포지션 손절이 가능합니다.

### 5. 일일 LLM 비용 한도

`llm.daily_cost_limit_usd` 도달 시 해당일 LLM 호출 전면 중단. 사이클은 계속 돌지만
후보 종목에 대한 Claude 판단 생성이 멈춥니다.

## 트러블슈팅

### "초당 거래건수를 초과하였습니다" (HTTP 500)

KIS는 rate limit 에러를 공식 문서와 달리 **HTTP 500 + JSON 바디**로 반환합니다. 본 봇의
`kis/client.py`는 바디를 먼저 파싱하고 `msg1`에 "초당" 포함 여부로 판정 후 0.5초 백오프
재시도합니다. 서버별 보수적 최소 호출 간격:

- 실전 서버: ≥ 0.12초 (≈ 8 req/s)
- 모의 서버: ≥ 0.55초 (≈ 1.8 req/s)

### 모의 서버에서 시세 조회가 종종 500

KIS 모의 서버(`openapivts`)의 국내주식 시세 API는 불안정합니다. 본 봇은 모드가 paper여도
**시세 조회는 항상 실전 서버(`openapi.koreainvestment.com:9443`)와 실전 키로** 보냅니다.
주문/잔고는 현재 모드 서버로. 이 이원화가 동작하려면 `.env`에 `KIS_LIVE_*` 키도 함께
세팅돼 있어야 합니다.

### "모의투자 장종료 입니다" (msg_cd=40580000)

KIS 모의 서버는 장외 시간 주문을 큐잉하지 않습니다. 스케줄러가 이미 평일 09:00~15:30
로 제한되어 있지만, `--once --force`로 강제 실행 시에도 주문은 거절됩니다.

### "Your credit balance is too low to access the Anthropic API"

Billing 페이지에 크레딧이 있는데도 이 에러가 나오면 **API 키를 재발급**하세요. 일부 케이스에서
서버 쪽에 "잔고 부족" 상태가 캐싱되어 크레딧 충전 후에도 해제되지 않는 현상이 관측됩니다.

### 텔레그램 `/start`만 보냈을 때 `getUpdates`가 비어있음

일부 Telegram 클라이언트는 `/start`를 특수 처리해서 서버에 전달 안 될 수 있습니다. 평문
메시지(예: `hi`) 하나 보내면 즉시 update가 잡힙니다.

### Claude API BadRequestError at tool_choice

Anthropic SDK 버전이 낮으면 `tool_choice={"type":"tool","name":...}` 포맷이 인식되지
않을 수 있습니다. `pip install --upgrade "anthropic>=0.40"` 로 최신화하세요.

## 프로젝트 구조

```
trading/
├─ Dockerfile                    python:3.11-slim, TZ=Asia/Seoul
├─ docker-compose.yml            volumes: data, logs, tokens, config(ro)
├─ pyproject.toml
├─ .env.example                  시크릿 템플릿
├─ .gitignore
├─ README.md
├─ config/
│   ├─ settings.yaml             유니버스, 사이클, 리스크, LLM 파라미터
│   └─ market_holidays.yaml      KRX 휴장일 (연 1회 갱신 필요)
├─ scripts/
│   ├─ stage3_verify.py          주문 실행 경로 E2E 검증
│   └─ stage4_verify.py          텔레그램 커맨드 핸들러 단위 검증
└─ trading_bot/
    ├─ main.py                   엔트리포인트 (스케줄러 + 폴러 기동)
    ├─ smoke_test.py             Stage 1 최소 검증 (토큰/잔고/시세/텔레그램)
    ├─ config.py                 .env + settings.yaml 통합 로딩
    ├─ logging_setup.py
    ├─ kis/
    │   ├─ auth.py               토큰 발급/캐시/자동 갱신
    │   └─ client.py             REST client (시세/잔고/OHLCV/주문/hashkey)
    ├─ signals/
    │   ├─ indicators.py         RSI, volume_ratio, SMA
    │   ├─ prefilter.py          룰베이스 후보 선정
    │   ├─ llm.py                Claude API + emit_decision tool_use
    │   └─ cycle.py              전체 오케스트레이션
    ├─ risk/
    │   ├─ manager.py            RiskManager (7단계 게이트)
    │   └─ kill_switch.py        파일 기반 킬 스위치
    ├─ bot/
    │   ├─ commands.py           텔레그램 커맨드 핸들러
    │   ├─ poller.py             long polling 백그라운드 스레드
    │   └─ context.py            BotContext (공유 상태 + trading_lock)
    ├─ store/
    │   ├─ db.py                 SQLite 스키마 + 마이그레이션
    │   └─ repo.py               insert/query 헬퍼
    ├─ notify/
    │   └─ telegram.py           sendMessage, getUpdates, inline keyboard
    └─ utils/
        └─ calendar_kr.py        한국 휴장일 + 장시간 판정
```

## 개발 로드맵

- [x] **Stage 1** — 골격 + KIS 인증 + 시세/잔고 + 텔레그램
- [x] **Stage 2** — 룰베이스 + Claude LLM 시그널 + DB
- [x] **Stage 3** — 주문 실행 + 리스크 매니저 + 킬 스위치
- [x] **Stage 4** — 텔레그램 양방향 제어
- [x] **Stage 5** — NAS Docker 배포
- [ ] **Stage 6** — 체결 확인 폴링 + 손절/익절 자동 청산
- [ ] **Stage 7** — 웹 대시보드 (FastAPI + 차트)
- [ ] **Stage 8** — Lean 엔진 백테스트 통합, 전략 튜닝

## 라이선스

Private use only. Not for redistribution.
