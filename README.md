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

배포는 **GitHub Container Registry(GHCR)** 기반입니다. `main` 브랜치로 push 하면 GitHub
Actions가 자동으로 Docker 이미지를 빌드해서 `ghcr.io/hdream0322/trading:latest` 로
푸시합니다. NAS는 이 이미지를 pull 받아 실행만 합니다 — 빌드 부담이 NAS에 없고,
Container Manager의 "업데이트" 버튼이 **정상 동작**합니다.

> ℹ️ 본 저장소와 GHCR 패키지는 public 입니다. 별도 인증 없이 `docker pull` 이 가능합니다.
> Private 으로 유지하고 싶으면 GHCR 로그인 절차가 추가되는데, 그 경우는 아래 "Private
> 패키지 사용 시" 항목 참고.

### 사전 준비 (최초 1회)

**1. DSM 패키지 설치**

DSM → **패키지 센터**에서 **Container Manager** 검색 후 설치 (DSM 7.2+). 이전 버전에서는
"Docker" 패키지. 설치 후 `/volume1/docker/` 공유 폴더가 자동 생성됩니다.

**2. SSH 서비스 활성화**

DSM → **제어판 → 터미널 및 SNMP → 터미널** 탭 → **SSH 서비스 활성화** 체크.

이때 **포트 번호 확인** 필수. 기본은 22지만 보안상 바꿔놓은 경우가 많습니다 (예: 2222).
나중에 `scp` / `ssh` 시 이 포트를 써야 합니다.

**3. SSH 접속 테스트**

맥북/PC 터미널에서:

```bash
ssh <계정>@<NAS-IP>
# SSH 포트가 22가 아니면:
ssh -p 2222 <계정>@<NAS-IP>
```

IP 확인: DSM → 제어판 → 정보 센터 → 네트워크. `DREAM.local` 같은 mDNS 이름도 종종 됩니다.

### 배포 절차

#### Step 1 — 프로젝트 폴더와 설정 파일 다운로드

NAS SSH 셸에서:

```bash
sudo mkdir -p /volume1/docker/trading/config
cd /volume1/docker/trading
sudo mkdir -p data logs tokens
```

이제 GitHub에서 `docker-compose.yml` 과 `config/` 아래 두 파일을 받습니다. **긴 URL을 한
줄에 붙여넣으면 터미널이 쪼개는 경우가 있어서** 변수로 나눠 실행하는 방식이 안전합니다.

```bash
BASE=https://raw.githubusercontent.com/hdream0322/trading/main
sudo curl -o docker-compose.yml           "$BASE/docker-compose.yml"
sudo curl -o config/settings.yaml         "$BASE/config/settings.yaml"
sudo curl -o config/market_holidays.yaml  "$BASE/config/market_holidays.yaml"
sudo chown -R $USER:users /volume1/docker/trading
ls -la
```

기대 결과: `docker-compose.yml` (약 700 B), `config/settings.yaml` (약 1.1 KB),
`config/market_holidays.yaml` (약 900 B), 빈 `data/` `logs/` `tokens/` 디렉터리.

#### Step 2 — `.env` 파일 NAS 로 전송

`.env` 는 시크릿이라 git 에 포함되지 않습니다. 로컬에서 직접 만든 파일을 NAS 로 올려야
합니다.

**맥북에서 새 터미널 창** 을 여세요 (NAS SSH 창은 그대로 유지). 그 다음:

```bash
scp -O -P 2222 /Users/dream/Documents/dev/trading/.env \
    <계정>@<NAS-IP>:/volume1/docker/trading/.env
```

플래그 설명:
- `-P 2222` — SSH 포트. **대문자 P** (소문자 p 는 다른 의미). 22 면 생략 가능.
- `-O` — 레거시 SCP 프로토콜 강제. Synology 는 기본으로 SFTP 서브시스템이 꺼져있어서
  최신 `scp`(내부적으로 SFTP 사용)는 `subsystem request failed on channel 0` 에러를
  냅니다. `-O` 가 이를 우회.

패스워드 입력 후 전송 완료되면 **NAS SSH 창으로 돌아가서** 권한을 600 으로 제한:

```bash
chmod 600 .env
ls -la .env
```

결과가 `-rw-------` 로 나오면 OK.

#### Step 3 — 이미지 pull 과 컨테이너 기동

```bash
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail 50 trading-bot
```

- `pull` 은 GHCR 에서 이미지 다운로드 (첫 실행 30초~2분, 이후는 캐시).
- `up -d` 는 detached 모드로 백그라운드 기동.
- `ps` 에서 `trading-bot` 이 `Up` 상태면 정상.
- 로그의 마지막 라인에 `Scheduler started` 가 보여야 하고, 이 시점에 텔레그램으로
  `*봇 기동* 🟡 모의 — 점검 10분 주기` 메시지가 도착합니다.

#### Step 4 — 최종 검증

핸드폰 텔레그램 앱에서 `@trading_deurim_bot` 채팅방 열고:

```
/status
```

응답이 오면 NAS 의 봇이 정상 동작하는 것입니다. 응답 메시지 하단에 인라인 버튼 4개
(`🛑 긴급 정지` `✅ 해제` `📊 내 주식` `💰 상태`) 가 붙어있어야 합니다.

### 업데이트 (원클릭)

코드 변경 후 `git push` 하면 GitHub Actions 가 1~3분 내에 새 이미지를 GHCR 에 올립니다.
NAS 에 반영하는 방법 3가지 중 하나:

**방법 A — Container Manager GUI (가장 쉬움)**
1. **Container Manager** 열기
2. **컨테이너** 탭 → `trading-bot` 클릭
3. **동작(Action) → 재설정(Reset)** 클릭
4. "이미지를 최신 버전으로 업데이트하시겠습니까?" 같은 확인창 → 예

**방법 B — SSH 한 줄**
```bash
cd /volume1/docker/trading && sudo docker compose pull && sudo docker compose up -d
```

**방법 C — DSM 작업 스케줄러 (자동 체크)**
작업 스케줄러에서 "User-defined script" 작업을 만들어 위 SSH 명령을 주기적으로(예: 매일
새벽 3시) 실행. 장외 시간대에 업데이트가 걸리니 안전.

### 자동 재기동

`docker-compose.yml` 에 `restart: unless-stopped` 가 들어있습니다. NAS 재부팅 / 컨테이너
크래시 / DSM 업데이트 후에도 봇이 자동으로 다시 떠집니다. 별도 설정 불필요.

### 중지 / 재시작

```bash
cd /volume1/docker/trading
sudo docker compose stop           # 일시 중지
sudo docker compose start          # 재시작
sudo docker compose down           # 완전 종료 (볼륨과 데이터는 유지)
sudo docker compose restart        # 재시작
sudo docker compose logs -f        # 실시간 로그 (Ctrl+C 로 빠져나옴)
```

또는 Container Manager GUI 에서 해당 컨테이너 선택 후 중지/시작/재시작 버튼.

### 데이터 영속성

`docker-compose.yml` 볼륨 매핑:
- `./data` → SQLite DB (`trading.sqlite`), KILL_SWITCH 파일
- `./logs` → `bot.log` 파일
- `./tokens` → KIS 액세스 토큰 캐시
- `./config` → 설정 파일 (읽기 전용)

**이미지 업데이트해도 위 볼륨은 유지**됩니다. 매매 이력, 긴급 정지 상태, 발급받은 토큰
전부 보존.

### Private 패키지 사용 시 (선택)

GHCR 패키지를 private 으로 두고 싶은 경우:

1. GitHub → **Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. **Generate new token** → Scopes 에서 `read:packages` 만 체크
3. 생성된 `ghp_...` 토큰 복사
4. NAS SSH 에서:
   ```bash
   echo "<PAT>" | sudo docker login ghcr.io -u <github-username> --password-stdin
   ```
5. `Login Succeeded` 확인. 이후 `docker compose pull` 이 정상 동작.
6. 자격증명은 `/root/.docker/config.json` 에 영속 저장.

### 트러블슈팅

**`scp: subsystem request failed on channel 0`**
Synology 에 SFTP 서브시스템이 꺼져있음. `scp -O` 로 레거시 프로토콜 사용 (위 Step 2
참고). 또는 DSM → File Station → 설정 → SFTP 탭 에서 SFTP 서비스 활성화.

**`ssh: Could not resolve hostname`**
mDNS 가 풀리지 않음. `DREAM.local` 또는 직접 IP 사용 (예: `192.168.0.148`). IP 는 DSM →
제어판 → 정보 센터 → 네트워크 탭에서 확인.

**`Connection refused` on port 22**
SSH 서비스 포트가 22가 아님. DSM → 제어판 → 터미널 및 SNMP 에서 포트 확인 후
`-p <포트>` (ssh) 또는 `-P <포트>` (scp) 로 지정.

**긴 URL 명령이 자동으로 줄바꿈됨**
`sudo curl -o 파일 URL` 에서 URL 부분이 터미널에서 잘려 두 줄로 해석되는 경우. 변수로
쪼개서 실행:
```bash
URL=https://raw.githubusercontent.com/.../파일.yaml
sudo curl -o 파일.yaml "$URL"
```

**`permission denied while trying to connect to Docker daemon socket`**
현재 사용자가 docker 그룹 멤버가 아님. `sudo` 붙여서 실행하거나, 그룹 추가:
```bash
sudo synogroup --add docker $USER
```
(DSM 에서 그룹 추가 후 재로그인)

**`pull access denied`**
GHCR 패키지가 private 상태거나 경로 오타. `docker-compose.yml` 의 `image:` 라인 확인.
Public 확인: 브라우저로 `https://ghcr.io/v2/hdream0322/trading/tags/list` 접속 시 태그
목록이 보이면 public.

**텔레그램 기동 메시지가 안 옴**
`.env` 의 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 오타 가능성. 컨테이너 내부 확인:
```bash
sudo docker compose exec trading-bot env | grep TELEGRAM
```

**봇 응답은 오는데 `/status` 에서 "잔고 조회 실패"**
KIS API 모의서버가 일시적으로 레이트리밋에 걸린 상태. 자동 재시도가 있지만 간혹 터짐.
몇 초 뒤 다시 `/status` 로 재시도.

**`모의투자 장종료 입니다` 에러**
KIS 모의서버는 장 시간(평일 09:00~15:30 KST) 외에는 주문을 받지 않습니다. 정상 동작.
다음 영업일 장 시작 후 자동으로 사이클이 돕니다.

### 완전 자동 업데이트 (Watchtower, 선택)

코드 push → 자동 재배포까지 원하면 Watchtower 컨테이너를 하나 더 띄웁니다. 몇 분마다
GHCR 확인해서 새 이미지 있으면 자동으로 재시작합니다. 장 중에 재시작이 걸릴 수 있어
주의 필요. 필요시 별도 설정.

### 버전 릴리스

프로젝트는 [semver](https://semver.org/lang/ko/) (`vMAJOR.MINOR.PATCH`)를 따릅니다.
릴리스를 찍으면 GitHub Actions가 자동으로:

1. Docker 이미지를 해당 버전 태그로 GHCR에 올림 (`:0.1.0`, `:0.1`, `:latest`)
2. GitHub Release를 생성하고 마지막 릴리스 이후의 커밋 로그를 자동 changelog로 첨부

**릴리스 찍는 법** (로컬에서):

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions 탭에서 `Create GitHub Release` 와 `Build and publish Docker image` 두
워크플로우가 동시에 돌고, 수 분 내에 Release 페이지에 새 릴리스가 생깁니다.

**특정 버전으로 배포 고정**:

운용 중 `:latest` 를 쓰면 push할 때마다 다음 `docker compose pull` 에서 바로 업데이트
됩니다. 안정성이 필요하면 `docker-compose.yml` 의 이미지 라인을 특정 버전으로 고정:

```yaml
services:
  trading-bot:
    image: ghcr.io/hdream0322/trading:v0.1.0   # latest 대신 특정 버전
```

이렇게 하면 새 릴리스가 나와도 자동 반영되지 않고, 수동으로 태그를 올려야 업데이트됩니다.
실전 계좌 운용 전환 시 이 방식을 권장합니다.

**롤백**:

```yaml
image: ghcr.io/hdream0322/trading:v0.0.9    # 이전 안정 버전으로 되돌림
```
후 `sudo docker compose pull && sudo docker compose up -d`

### 중지 / 재시작

```bash
sudo docker compose stop           # 임시 중지
sudo docker compose start          # 재시작
sudo docker compose down           # 완전 종료 (볼륨은 유지)
sudo docker compose restart        # 컨테이너 재시작
```

### Container Manager GUI에서 관리

Container Manager → **컨테이너** 탭 → `trading-bot` 선택 → 로그, 재시작, 중지, 재설정
버튼 사용. **프로젝트** 탭에서 docker-compose.yml 기반 관리도 가능.

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

### 6. 자동 청산 (손절/익절/트레일링 스톱)

보유 포지션 각각에 대해 **매 점검마다** 세 가지 기계적 규칙을 체크합니다. 어느 하나라도
충족하면 AI 판단을 건너뛰고 즉시 시장가 판매. 청산 판매는 `daily_loss_limit`,
`max_orders_per_day`, 킬 스위치 영향을 받지 않습니다 (포지션 보호가 최우선).

- **🛡️ 손실 차단** (`stop_loss_pct`, 기본 -5%): 손익률이 임계값 이하로 떨어지면 즉시 매도
- **🎯 이익 확정** (`take_profit_pct`, 기본 +15%): 손익률이 임계값 이상이면 즉시 매도
- **📉 트레일링 스톱** (`trailing_activation_pct`/`trailing_distance_pct`, 기본 +7%/-4%):
  보유 중 최고 손익률이 +7% 를 한 번이라도 넘으면 트레일링이 활성화되고, 그 이후 최고점
  대비 -4% 떨어지면 자동 판매

예시 (기본 설정):
- 20만 원에 구매 → 21만 4천 원(+7%) 도달 → 트레일링 활성화
- 22만 원(+10%) 까지 상승 후 21만 1천 2백 원(-4% from hwm) 로 하락 → 자동 판매 (+5.6% 수익)

트레일링 스톱은 **이익은 살리고 추세 전환만 잡는** 장치입니다. 이익 확정(+15%)에 닿기 전에
추세가 꺾여도 활성화 조건만 넘었다면 손실 없이 정리됩니다.

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
- [x] **Stage 6** — 손절/익절/트레일링 스톱 자동 청산
- [ ] **Stage 7** — 웹 대시보드 (FastAPI + 차트)
- [ ] **Stage 8** — Lean 엔진 백테스트 통합, 전략 튜닝

## 라이선스

Private use only. Not for redistribution.
