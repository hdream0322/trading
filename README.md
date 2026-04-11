# KIS Auto Trading Bot

한국투자증권(KIS) Open API와 Anthropic Claude API를 결합한 **국내주식 자동매매 봇**입니다.
룰베이스 사전필터 + LLM 독립 판단 + 교차검증 + 리스크 매니저의 **4중 게이트** 구조로 동작하며,
텔레그램 하나로 전 기능 원격 제어가 가능하고 Synology NAS Docker 배포를 기본 상정합니다.

> ⚠️ 이 봇은 실제 돈을 움직일 수 있습니다. 반드시 모의투자(`KIS_MODE=paper`)로 최소 4주간
> 검증한 뒤 실전 전환 여부를 판단하세요. 제작자는 어떤 손실에도 책임지지 않습니다.

---

## 📌 목차

- [주요 기능](#-주요-기능)
- [아키텍처](#-아키텍처)
- [빠른 시작 (로컬)](#-빠른-시작-로컬)
- [처음 시작하기 (외부 계정 준비)](#-처음-시작하기-외부-계정-준비) _접힘_
- [Docker 배포 (Synology NAS)](#-docker-배포-synology-nas) _접힘_
- [설정](#-설정)
- [텔레그램 커맨드](#-텔레그램-커맨드)
- [안전장치](#-안전장치)
- [트러블슈팅](#-트러블슈팅) _접힘_
- [프로젝트 구조](#-프로젝트-구조) _접힘_
- [개발 로드맵](#-개발-로드맵)
- [라이선스](#-라이선스) _접힘_

---

## ✨ 주요 기능

- **4중 판단 게이트** — 룰베이스 prefilter(추세 필터 포함) → Claude LLM 독립 판단 → prefilter↔LLM 교차검증 → 8단계 리스크 매니저
- **ATR 기반 동적 손절** — 변동성 큰 종목은 자동으로 더 넓은 손절 폭 적용 (`max(5%, ATR×1.5/가격×100)`)
- **섹터 분산** — universe 에 KIS 업종명 자동 백필, 동일 섹터 최대 2종목 제한으로 집중 리스크 분산
- **자동 청산** — 손절/익절/트레일링 스톱을 AI 우회 기계적 규칙으로 즉시 실행
- **사후 정확도 트래킹** — 5 거래일 경과한 AI 판단의 forward return 을 자동 계산, `/accuracy` 로 confidence bucket 별 적중률 확인
- **체결 확인** — 주문 후 30초 일괄 대기 → 미체결 매수 자동 취소, 미체결 판매는 계속 대기
- **자동 복구 회로차단기** — 에러 급증 감지 시 자동 킬스위치 활성화 + 에러 진정 시 자동 해제 (플래핑 방지)
- **이원화된 서버 경로** — 시세는 항상 실전 서버, 주문/잔고는 현재 모드 서버 (모의 서버 시세 API 불안정 해결)
- **KIS rate limit 자동 재시도** — "초당 거래건수 초과"(HTTP 500) 백오프 재시도, `settings.yaml` 에서 간격 조정 가능
- **런타임 자격증명 교체** — `/setcreds`, `/mode`, `/reload`, `/restart` 로 Docker 재시작 없이 앱키/모드 전환
- **텔레그램 18개 커맨드** — 멀티라인 커맨드 지원, 인라인 버튼, chat_id 화이트리스트
- **한국 휴장일 캘린더** — KRX 휴장일 YAML 관리, 주말/휴장일 자동 스킵 + 주간 리마인더
- **일일 SQLite 백업** — 매일 01:55 KST 롤링 백업 (7일 보관)
- **Docker 기반 자동 배포** — Watchtower + GHCR 파이프라인, main push → tag 푸시 → NAS 자동 반영

---

## 🏗 아키텍처

```
┌─ APScheduler (평일 09:00~15:30 KST, 10분 주기) ──────────────────┐
│                                                               │
│  1. 잔고 조회 (KIS inquire-balance, 현재 모드 서버)                 │
│  2. 포지션 상태 동기화 (entry_price, high_water_mark)              │
│                                                               │
│  3. 🛡️ 자동 청산 체크 (보유 각각)                                   │
│     ├─ 손실 차단: pnl ≤ -max(5%, ATR×1.5/가격×100)               │
│     ├─ 이익 확정: pnl ≥ +15%                                    │
│     └─ 트레일링: +7% 도달 후 고점 대비 -4% 하락 시                    │
│     → 리스크 게이트(is_exit=True) → 시장가 전량 판매                 │
│                                                               │
│  4. 변동성 구간 체크 (09:00~09:10, 15:20~15:30)                   │
│     → 신규 매수만 차단, 청산은 그대로                                │
│                                                               │
│  5. 유니버스 셔플 (seed 로깅 — LLM 비용 한도 편향 제거)                │
│                                                               │
│  6. 종목별 신규 진입 검토 (4-Gate):                                │
│     ├─ Gate 1 — 룰베이스 prefilter                              │
│     │   buy: RSI<35 AND 거래량≥1.2x AND 현재가>SMA20             │
│     │   sell: RSI>70 AND 거래량≥1.2x                           │
│     ├─ Gate 2 — Claude LLM (tool_use, side_hint 없음)          │
│     │   → decision/confidence/reasoning 독립 판단               │
│     ├─ Gate 3 — confidence ≥ 0.75                             │
│     ├─ Gate 3.5 — 교차검증 (prefilter ↔ LLM 불일치 reject)        │
│     └─ Gate 4 — 리스크 매니저 8단계                                │
│         • 킬 스위치 (매수만 차단)                                  │
│         • 일일 주문 수 한도                                       │
│         • 일일 손실 한도 (-3%)                                   │
│         • 종목별 쿨다운 (60분)                                    │
│         • 중복 진입 차단                                         │
│         • 동시 보유 ≤ 5                                         │
│         • 섹터 분산 (동일 업종 ≤ 2)                               │
│         • 포지션 사이징 (총자산 × 19.5%)                           │
│                                                               │
│  7. KIS 시장가 주문 (hashkey 선발급)                              │
│  8. 30초 대기 후 체결 확인 → 미체결 매수 자동 취소                     │
│  9. DB 기록 + Telegram 알림                                     │
└───────────────────────────────────────────────────────────────┘
       ▲                              ▼
       │                    [SQLite trading.sqlite]
┌──────┴────────────┐       signals / orders / errors
│ Telegram Poller   │       position_state / pnl_daily
│ long polling 스레드 │                    │
│ 18 커맨드 + 버튼     │       ┌────────────┴────────────┐
│ 멀티라인 지원         │      │ 9 크론 (백업/복구/브리핑     │
└───────────────────┘       │  /만료/자격증명/휴장일/      │
                            │  체결 확인/사후 정확도)      │
                            └─────────────────────────┘
```

---

## 🚀 빠른 시작 (로컬)

```bash
git clone https://github.com/hdream0322/trading.git
cd trading

# 시크릿 파일 생성
cp .env.example .env
chmod 600 .env
# 에디터로 .env 열고 키 채우기 (아래 '처음 시작하기' 참고)

# 의존성 설치
python3 -m venv .venv
source .venv/bin/activate
pip install "httpx[http2]>=0.27" "pyyaml>=6.0" "python-dotenv>=1.0" \
            "anthropic>=0.40" "apscheduler>=3.10,<4"

# Stage 1 smoke test (토큰 → 잔고 → 시세 → 텔레그램)
python -m trading_bot.smoke_test

# 단일 사이클 수동 실행 (장외 시간도 강제 실행)
python -m trading_bot.main --once --force

# 스케줄러 모드 (평일 09:00~15:30 KST 자동)
python -m trading_bot.main
```

---

## 🔑 처음 시작하기 (외부 계정 준비)

봇을 돌리려면 **세 가지 외부 서비스** 계정/키가 필요합니다. 전부 오늘 안에 발급 가능합니다.

| 서비스 | 용도 | 비용 |
|---|---|---|
| 한국투자증권 Open API | 주식 시세, 주문 실행 | 무료 (모의 계좌 가상자금) |
| Anthropic Claude API | AI 매매 판단 | $5 충전 시 수천 회 호출 (Haiku 4.5 기준) |
| Telegram Bot | 알림 + 원격 제어 | 무료 |

<details>
<summary><b>1단계. 한국투자증권 (KIS) Open API 앱키 발급</b></summary>

> 실전 주식 계좌가 없어도 OK. 비대면 계좌 개설을 먼저 하세요. 모의투자는 실전 계좌가
> 있으면 자동으로 생성됩니다.

**1-1. 회원가입 및 계좌**
1. [한국투자증권 공식 사이트](https://securities.koreainvestment.com) 회원가입
2. 모바일 앱 또는 PC 웹으로 **비대면 계좌 개설** (신분증 + 본인 명의 은행 계좌)
3. 계좌번호 받음 (예: `50181827`, 8자리 + 상품코드 `01`)

**1-2. Open API 서비스 신청**
1. [KIS Developers](https://apiportal.koreainvestment.com) 접속 → 로그인
2. **KIS Developers → 신청/해지** 메뉴
3. **OPEN API 서비스 신청** 클릭 → 약관 동의

**1-3. 앱키 발급 (모의투자용)**
1. **KIS Developers → 앱 관리 → 앱 생성**
2. 앱 이름: 아무거나 (예: `trading-bot`)
3. 거래 유형: **모의투자** 선택
4. 생성되면 `APP KEY` 와 `APP SECRET` 표시 → **메모장에 저장** (시크릿은 재확인 불가)

**1-4. 모의투자 계좌번호 확인**
1. 한국투자증권 앱 로그인 → 계좌 목록에서 **모의투자** 계좌 번호 확인
2. `XXXXXXXX-YY` 형식이면 `XXXXXXXX` = `KIS_PAPER_ACCOUNT_NO`, `YY` = `KIS_PAPER_ACCOUNT_PRODUCT_CD` (보통 `01`)

> 💡 실전 키도 함께 등록해두는 것을 권장 — 시세 조회는 항상 실전 서버로 가기 때문.
> 모드는 `paper` 유지하면 주문은 모의로만 나갑니다.

> ℹ️ **모의 계좌는 90일 유효** — 만료되면 재신청 필요. 봇이 7일 전부터 자동 경고하고
> `/setcreds paper ...` 한 줄로 런타임 교체 가능합니다 (Docker 재시작 불필요).

</details>

<details>
<summary><b>2단계. Anthropic Claude API 키 발급</b></summary>

**2-1. 가입 + 크레딧 충전**
1. [console.anthropic.com](https://console.anthropic.com) 회원가입 (구글 계정 가능)
2. **Plans & Billing → Credits** → Add credits → **최소 $5 충전**
3. 결제 수단 등록 (신용카드 또는 Link by Stripe)
4. Billing 페이지에서 `Credit balance: $5.00` 확인

**2-2. API 키 생성**
1. **Settings → API keys → Create Key**
2. 이름: `trading-bot`
3. 생성된 `sk-ant-api03-...` 토큰 **복사해서 메모장 저장** (재확인 불가)

> ⚠️ 신규 계정에서 크레딧이 있는데도 "Credit balance too low" 에러가 나오면 API 키를
> 재발급해보세요. 간혹 서버 캐시 문제로 기존 키가 잔고 부족 상태로 잠길 수 있습니다.

> 💡 **프롬프트 캐싱** 덕분에 실제 비용은 Haiku 4.5 기준 월 $1~2 수준입니다.
> 10분 주기 × 영업일 × 10종목이어도 system + tool 정의가 캐시 히트로 처리됩니다.

</details>

<details>
<summary><b>3단계. Telegram 봇 생성 + chat_id 확인</b></summary>

**3-1. 봇 만들기**
1. Telegram 앱 열기 → 상단 검색창에 `@BotFather` 검색 → 채팅
2. `/newbot` 전송
3. 봇 이름 입력 (자유, 예: `My Trading Bot`)
4. 봇 사용자명 입력 (영문, `_bot` 으로 끝나야 함, 예: `my_trading_123_bot`)
5. 성공하면 토큰이 출력됨:
   ```
   1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ1234567890
   ```
   이 토큰이 `TELEGRAM_BOT_TOKEN` 값입니다.

**3-2. chat_id 확인**
1. 방금 만든 봇을 Telegram 검색으로 찾아서 채팅창 열기
2. **아무 메시지나** 하나 보내기 (예: `hi`) — `/start` 는 가끔 전달이 안 되니 평문으로
3. 브라우저에서 아래 URL 열기 (토큰 부분만 바꿔서):
   ```
   https://api.telegram.org/bot<토큰>/getUpdates
   ```
4. JSON 응답에서 `"chat":{"id":123456789, ...}` 부분의 **숫자가 chat_id**
5. 그 숫자가 `TELEGRAM_CHAT_ID` 값

</details>

<details>
<summary><b>4단계. <code>.env</code> 파일 작성</b></summary>

프로젝트 루트에 `.env` 파일을 만들고 위에서 수집한 값들을 채웁니다.

```bash
cd trading
cp .env.example .env
chmod 600 .env
nano .env   # 또는 vim, code .env 등
```

**`.env` 파일 내용 예시**:
```dotenv
# 운용 모드 — 처음에는 반드시 paper 로 시작
KIS_MODE=paper

# 모의투자 계좌 (1단계에서 발급)
KIS_PAPER_APP_KEY=PSXXXxxxxXXXXxxxXXXX
KIS_PAPER_APP_SECRET=longBase64String...
KIS_PAPER_ACCOUNT_NO=50181827
KIS_PAPER_ACCOUNT_PRODUCT_CD=01

# 실전 키 — 있으면 넣어두고 KIS_MODE 만 바꾸면 전환됨
# 시세 조회는 모드가 paper 여도 실전 키로 하기 때문에 같이 넣어두는 걸 권장
KIS_LIVE_APP_KEY=PSYYYyyyyYYYYyyyYYYY
KIS_LIVE_APP_SECRET=longBase64String...
KIS_LIVE_ACCOUNT_NO=47375928
KIS_LIVE_ACCOUNT_PRODUCT_CD=01

# Anthropic (2단계)
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Telegram (3단계)
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=123456789

# 런타임
TZ=Asia/Seoul
LOG_LEVEL=INFO
```

**보안 체크리스트**:
- [ ] `.env` 파일 권한 `600` (`chmod 600 .env`)
- [ ] `.gitignore` 에 `.env` 포함 확인 (이미 포함돼 있음)
- [ ] `.env` 를 실수로 슬랙/이메일/채팅에 붙여넣지 않기
- [ ] 만약 키가 유출됐다면 **즉시 재발급** (KIS 앱 관리, Anthropic Settings, BotFather `/revoke`)

</details>

---

## 🐳 Docker 배포 (Synology NAS)

배포는 **GitHub Container Registry(GHCR)** 기반입니다. `v*` 태그를 푸시하면 GitHub Actions
가 자동으로 `ghcr.io/hdream0322/trading:latest` 로 이미지를 올리고, NAS 의 **Watchtower** 가
매일 02:00 KST 에 감지해 자동 배포합니다. 수동 SSH 없이 운용 가능합니다.

> ℹ️ 본 저장소와 GHCR 패키지는 public 입니다. 별도 인증 없이 `docker pull` 가능합니다.

<details>
<summary><b>사전 준비 (최초 1회)</b></summary>

**1. DSM 패키지 설치**

DSM → **패키지 센터**에서 **Container Manager** 검색 후 설치 (DSM 7.2+). 이전 버전에서는
"Docker" 패키지. 설치 후 `/volume1/docker/` 공유 폴더가 자동 생성됩니다.

**2. SSH 서비스 활성화**

DSM → **제어판 → 터미널 및 SNMP → 터미널** 탭 → **SSH 서비스 활성화** 체크.
이때 **포트 번호 확인** 필수 (기본 22, 변경 가능).

**3. SSH 접속 테스트**

```bash
ssh <계정>@<NAS-IP>
# SSH 포트가 22가 아니면:
ssh -p 2222 <계정>@<NAS-IP>
```

IP 확인: DSM → 제어판 → 정보 센터 → 네트워크.

</details>

<details>
<summary><b>배포 절차 (Step 1~4)</b></summary>

#### Step 1 — 프로젝트 폴더와 설정 파일 다운로드

NAS SSH 셸에서:

```bash
sudo mkdir -p /volume1/docker/trading/config
cd /volume1/docker/trading
sudo mkdir -p data logs tokens
```

GitHub에서 `docker-compose.yml` 과 `config/` 아래 두 파일을 받습니다:

```bash
BASE=https://raw.githubusercontent.com/hdream0322/trading/main
sudo curl -o docker-compose.yml           "$BASE/docker-compose.yml"
sudo curl -o config/settings.yaml         "$BASE/config/settings.yaml"
sudo curl -o config/market_holidays.yaml  "$BASE/config/market_holidays.yaml"
sudo chown -R $USER:users /volume1/docker/trading
ls -la
```

#### Step 2 — `.env` 파일 NAS 로 전송

`.env` 는 시크릿이라 git 에 포함되지 않습니다. 로컬에서 직접 만든 파일을 NAS 로 올립니다.

**맥북에서 새 터미널 창** (NAS SSH 창은 유지):

```bash
scp -O -P 2222 /Users/dream/Documents/dev/trading/.env \
    <계정>@<NAS-IP>:/volume1/docker/trading/.env
```

- `-P 2222` — SSH 포트 (대문자 P)
- `-O` — 레거시 SCP 프로토콜 강제 (Synology SFTP 서브시스템이 꺼져있어 필요)

전송 후 NAS SSH 창에서 권한 제한:

```bash
chmod 600 .env
```

#### Step 3 — 이미지 pull 과 컨테이너 기동

```bash
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail 50 trading-bot
```

로그 마지막에 `Scheduler started` 가 보이면 OK. 동시에 텔레그램으로
`*봇 기동* 🟡 모의 — 점검 10분 주기` 메시지가 도착합니다.

#### Step 4 — 최종 검증

텔레그램에서 `/status` 전송. 응답이 오면 정상. 하단에 인라인 버튼 4개
(`🛑 긴급 정지` `✅ 해제` `📊 내 주식` `💰 상태`) 가 붙어있어야 합니다.

</details>

<details>
<summary><b>업데이트 (Watchtower 자동 / SSH 수동 / GUI)</b></summary>

**방법 A — 🤖 자동 업데이트 (Watchtower, 기본)**

`docker-compose.yml` 의 Watchtower 가 매일 **02:00 KST** 에 GHCR 에서 새 이미지를 감지하고
자동 pull → 컨테이너 교체. 대상은 `com.centurylinklabs.watchtower.enable=true` label 이
붙은 `trading-bot` 컨테이너만.

```
git push origin v0.x.y
  ↓
GitHub Actions 1~3분 내 이미지 빌드 → GHCR :latest 업데이트
  ↓
익일 02:00 KST, NAS Watchtower 감지
  ↓
SIGTERM → Python 깔끔하게 종료 → 새 이미지 pull → 새 컨테이너
  ↓
Telegram 알림 2건: Watchtower 리포트 + 봇 기동 메시지
```

**방법 B — SSH 한 줄 (즉시)**
```bash
cd /volume1/docker/trading && sudo docker compose pull && sudo docker compose up -d
```

**방법 C — Container Manager GUI**
컨테이너 탭 → `trading-bot` → 동작(Action) → 재설정(Reset)

**방법 D — 텔레그램에서 `/update confirm`**
`/update` 로 현재/최신 digest 비교 후 `/update confirm` 으로 즉시 반영 (Watchtower HTTP API).

</details>

<details>
<summary><b>Watchtower 설정 / 자동 재기동 / 중지·재시작 / 데이터 영속성 / Private 패키지</b></summary>

### Watchtower 설정 커스터마이즈

`docker-compose.yml` 의 watchtower 서비스 환경변수:

| 변수 | 기본값 | 설명 |
|---|---|---|
| `WATCHTOWER_SCHEDULE` | `0 0 2 * * *` | cron 6필드. 매일 02:00 KST |
| `WATCHTOWER_LABEL_ENABLE` | `true` | label 붙은 컨테이너만 감시 |
| `WATCHTOWER_CLEANUP` | `true` | 오래된 이미지 자동 삭제 |
| `WATCHTOWER_NOTIFICATION_REPORT` | `true` | 결과를 하나의 알림으로 요약 |
| `WATCHTOWER_NO_STARTUP_MESSAGE` | `true` | Watchtower 기동 시 알림 생략 |
| `WATCHTOWER_NOTIFICATION_URL` | telegram 자동 주입 | shoutrrr 포맷 |

체크 주기 바꾸기 예:
- 매일 새벽 3시: `0 0 3 * * *`
- 매주 일요일 자정: `0 0 0 * * 0`

자동 업데이트를 끄려면 텔레그램 `/update disable`, 또는 `docker-compose.yml` 에서
watchtower 서비스 블록 주석 처리 후 `docker compose up -d`.

### 자동 재기동

`docker-compose.yml` 에 `restart: unless-stopped` 가 들어있어 NAS 재부팅/크래시/DSM 업데이트
후에도 봇이 자동으로 다시 떠집니다.

### 중지 / 재시작

```bash
cd /volume1/docker/trading
sudo docker compose stop           # 일시 중지
sudo docker compose start          # 재시작
sudo docker compose down           # 완전 종료 (볼륨과 데이터는 유지)
sudo docker compose restart        # 재시작
sudo docker compose logs -f        # 실시간 로그 (Ctrl+C 로 빠져나옴)
```

또는 텔레그램 `/restart` (Python `SIGTERM` → Docker restart 정책).

### 데이터 영속성

`docker-compose.yml` 볼륨 매핑:
- `./data` → SQLite DB, KILL_SWITCH, credentials.env, universe.json, paper_account_issued, 백업 등
- `./logs` → `bot.log`
- `./tokens` → KIS 액세스 토큰 캐시
- `./config` → 설정 파일 (읽기 전용)

**이미지 업데이트해도 위 볼륨은 유지**됩니다. 매매 이력, 킬스위치 상태, 자격증명, 토큰 전부 보존.

### Private 패키지 사용 시

GHCR 패키지를 private 으로 두려면:

1. GitHub → **Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. **Generate new token** → Scopes `read:packages` 체크
3. NAS SSH 에서:
   ```bash
   echo "<PAT>" | sudo docker login ghcr.io -u <github-username> --password-stdin
   ```
4. `Login Succeeded` 확인 → 이후 `docker compose pull` 정상 동작

</details>

---

## ⚙️ 설정

### `.env` — 시크릿

```bash
KIS_MODE=paper                    # paper | live
KIS_PAPER_APP_KEY=...
KIS_PAPER_APP_SECRET=...
KIS_PAPER_ACCOUNT_NO=...          # 8자리 계좌번호
KIS_PAPER_ACCOUNT_PRODUCT_CD=01
KIS_LIVE_APP_KEY=...              # (선택) 실전 키 — 모드 토글용
KIS_LIVE_APP_SECRET=...
KIS_LIVE_ACCOUNT_NO=...

ANTHROPIC_API_KEY=...             # Claude API
TELEGRAM_BOT_TOKEN=...             # BotFather 발급
TELEGRAM_CHAT_ID=...               # 본인 채팅 ID

TZ=Asia/Seoul
LOG_LEVEL=INFO
```

> `.env` 는 `.gitignore` 에 포함되어 있으며 **절대 커밋하지 마세요**. 파일 권한 `600`.

<details>
<summary><b><code>config/settings.yaml</code> — 운용 파라미터 (v0.5.0)</b></summary>

```yaml
mode: paper  # KIS_MODE 환경변수가 우선

universe:
  - {code: "005930", name: "삼성전자"}
  - {code: "000660", name: "SK하이닉스"}
  - {code: "035720", name: "카카오"}
  - {code: "035420", name: "NAVER"}
  - {code: "005380", name: "현대차"}
  - {code: "005490", name: "POSCO홀딩스"}
  - {code: "051910", name: "LG화학"}
  - {code: "068270", name: "셀트리온"}
  - {code: "055550", name: "신한지주"}
  - {code: "015760", name: "한국전력"}

cycle_minutes: 10                    # 사이클 주기
market_hours:
  open: "09:00"
  close: "15:30"

# KIS API throttle (초 단위)
# 실전 공식 한도 20 req/s, 모의 2 req/s. 기본값은 한도의 90% 안전 마진.
# 신규 API 발급 후 3일간은 실전 3 req/s 임시 제한 → 그 기간엔 live_min_interval_sec 를 0.34 로.
rate_limit:
  live_min_interval_sec: 0.055       # ≈18 req/s
  paper_min_interval_sec: 0.55       # ≈1.8 req/s

risk:
  max_position_per_symbol_pct: 19.5  # 종목당 총자산 최대 비중 (5종목 × 19.5% = 97.5%)
  max_concurrent_positions: 5        # 동시 보유 최대 종목 수
  max_per_sector: 2                  # 동일 섹터(업종) 최대 보유 종목 수
  daily_loss_limit_pct: 3            # 전일 대비 -X% 손실 시 신규 매수 차단
  cooldown_minutes: 60               # 같은 종목 재진입 대기
  max_orders_per_day: 6              # 일일 총 주문 건수 상한

llm:
  model: "claude-haiku-4-5-20251001"
  temperature: 0
  confidence_threshold: 0.75         # 시그널 발효 임계값
  daily_cost_limit_usd: 5
  input_price_per_mtok: 1.0
  output_price_per_mtok: 5.0

prefilter:
  rsi_period: 14
  rsi_buy_below: 35
  rsi_sell_above: 70
  min_volume_ratio: 1.2              # 20일 평균 대비 거래량 최소 배수
  trend_filter_enabled: true         # 추세 필터: 현재가 > SMA 미만이면 매수 후보 제외
  trend_sma_period: 20

# Stage 6: 자동 청산 (손절/익절/트레일링 + ATR 동적 손절)
exit:
  stop_loss_pct: 5                   # 🛡️ 고정 손절 하한 (%)
  take_profit_pct: 15                # 🎯 이익 확정 (%)
  trailing_activation_pct: 7         # 📉 트레일링 활성 임계값
  trailing_distance_pct: 4           # 📉 고점 대비 낙폭
  atr_enabled: true                  # ATR 동적 손절 활성화
  atr_period: 14
  atr_multiplier: 1.5                # 실제 손절 = max(stop_loss_pct, ATR×1.5/가격×100)
```

</details>

### `config/market_holidays.yaml` — 휴장일

한국거래소(KRX) 공식 달력을 매년 갱신. 매주 월요일 07:00 KST 에 주간 리마인더 알림이 옵니다
(임시공휴일 대응).

---

## 💬 텔레그램 커맨드

총 **18개** 커맨드. 멀티라인(한 말풍선에 여러 `/커맨드` 줄바꿈) 순차 실행 지원.

### 시작
| 커맨드 | 기능 |
|---|---|
| `/menu`, `/start` | 메인 허브 (자주 쓰는 동작을 버튼 하나로) |

### 조회
| 커맨드 | 기능 |
|---|---|
| `/help` | 커맨드 목록 |
| `/status` | 모드·총자산·킬스위치·LLM 비용 + 퀵 액션 버튼 |
| `/positions` | 갖고 있는 주식 (수량/평단/현재가/평가손익) + 종목별 판매 버튼 |
| `/signals` | 오늘 매매 추천 (최근 10개) |
| `/accuracy` | **AI 판단 사후 적중률** (confidence bucket 별 + 교차검증 집계) |
| `/cost` | 오늘 LLM 누적 비용 / 한도 대비 % |
| `/mode` | 거래 모드 조회 + 전환 버튼 |
| `/universe` | 추적 종목 목록 (현재가 + 섹터) |
| `/about` | 봇 정보, 버전, 아키텍처 |

### 조작
| 커맨드 | 기능 |
|---|---|
| `/stop`, `/kill` | 🛑 킬 스위치 활성 (신규 매수 차단) |
| `/resume` | ✅ 킬 스위치 해제 |
| `/quiet` | 🔕 조용 모드 토글 (hold-only 10분 요약 끔) |
| `/sell` | 보유 종목 판매 버튼 (또는 `/sell CODE` 직접) |
| `/cycle` | 사이클 1회 즉시 실행 |
| `/universe add CODE` | 종목 추가 (KIS 이름·업종 자동 조회) |
| `/universe remove` | 종목 제거 (버튼 목록) |

### 업데이트
| 커맨드 | 기능 |
|---|---|
| `/update` | 현재/최신 버전 비교 |
| `/update confirm` | 최신 버전으로 즉시 업데이트 (Watchtower) |
| `/update notes [버전]` | 릴리스 노트 |
| `/update enable`/`disable`/`status` | 자동 업데이트 토글 |

### 자격증명
| 커맨드 | 기능 |
|---|---|
| `/setcreds paper KEY SECRET ACCOUNT` | 모의 키 런타임 교체 (원본 메시지 자동 삭제) |
| `/setcreds live KEY SECRET ACCOUNT confirm` | 실전 교체 (confirm 필수) |
| `/reload` | `data/credentials.env` 수동 재로드 |
| `/restart` | 컨테이너 재시작 (`SIGTERM` → Docker 재기동) |

사이클 요약 메시지에는 자동으로 `[🛑 긴급 정지] [✅ 해제] [📊 포지션] [💰 상태]` 인라인 버튼이
첨부되어 한 탭으로 조작 가능합니다.

---

## 🛡 안전장치

### 1. 모드 분리 (paper / live)
`.env` 의 `KIS_MODE=paper` 가 기본값. 실전 전환은 최소 4주 모의 검증 후에만 권장.
paper/live 키를 `.env` 에 동시에 저장하고 `KIS_MODE` 한 줄(또는 `/mode live confirm`) 만 바꿔
전환하는 구조 — 키 교체 중 실수로 실전에 쏘는 사고 예방.

### 2. 4중 판단 게이트
1. **룰베이스 prefilter** — RSI + 거래량 배수 + 추세 필터(`현재가 > SMA20`). 중립 종목은 LLM 호출 없이 hold → 비용 절약.
2. **Claude LLM 독립 판단** — `temperature=0` + `tool_use` 구조화 출력. **side_hint 미전달** 로 확증 편향 제거.
3. **Confidence 임계값** — `confidence_threshold` (기본 0.75) 미만은 주문 안 함.
4. **교차검증** — prefilter 방향과 LLM decision 이 어긋나면 주문 생략 + DB 에 `DIRECTION_CONFLICT` / `LLM_HOLD` 태그 기록. 사후 `/accuracy` 로 검증.

### 3. 8단계 리스크 매니저
모든 주문은 `RiskManager.check()` 를 통과해야 실행:

1. side 검증 (buy/sell 만)
2. 킬 스위치 (매수 차단, 매도 허용)
3. 일일 주문 수 한도
4. 일일 손실 한도 (매수만)
5. 종목별 쿨다운
6. 중복 진입 차단
7. 동시 보유 종목 수
8. **섹터(업종) 분산 한도** — 동일 섹터 `max_per_sector` (기본 2) 도달 시 차단
9. 포지션 사이징

### 4. 자동 청산 — 기계적 규칙 (LLM 우회)
보유 포지션 각각에 대해 **매 점검마다** 세 가지 규칙을 체크. 어느 하나라도 충족하면 즉시
시장가 판매. 청산 판매는 `daily_loss_limit`, `max_orders_per_day`, 킬 스위치 영향을 받지 않음
(포지션 보호가 최우선).

- **🛡️ 손실 차단** (`stop_loss_pct`): `max(5%, ATR×1.5/가격×100)` — ATR 기반 동적 손절.
  변동성 작은 종목은 고정 5%, 변동성 큰 종목(2차전지/바이오 등)은 자동으로 더 넓은 폭 적용.
- **🎯 이익 확정** (`take_profit_pct`, 기본 +15%): 손익률이 임계값 이상이면 즉시 판매
- **📉 트레일링 스톱** (`trailing_activation_pct`/`distance_pct`, 기본 +7%/-4%):
  최고 손익률이 +7% 를 한 번이라도 넘으면 트레일링 활성화 → 최고점 대비 -4% 떨어지면 판매

예시: 20만 원 구매 → 21만 4천 원(+7%) 트레일링 활성화 → 22만 원(+10%) 까지 상승 후
21만 1천 2백 원(-4% from hwm) 로 하락 → 자동 판매 (+5.6% 수익)

### 5. 킬 스위치 (수동 + 자동)
파일 기반(`data/KILL_SWITCH`) + 텔레그램 `/stop`/`/resume` + **회로차단기**. 매수만 차단되고
매도는 허용되어 킬스위치 활성 중에도 자동 청산은 계속 돕니다.

**회로차단기 자동 복구** (v0.5.0):
- 5분마다 최근 1시간 에러 카운트 체크, ≥10건이면 **자동 킬스위치** + 긴급 알림
- 자동 활성화 + 15분 경과 + 최근 30분 에러 0건이면 **자동 해제** + 복구 알림
- 최근 1시간 내 자동 해제 이력이 있으면 재활성화 시 "수동 해제만" 경고 (플래핑 방지)
- 수동으로 건 킬스위치는 절대 자동 해제 안 함

### 6. 장 시작/마감 변동성 구간 차단
09:00~09:10 (호가 형성 직후) / 15:20~15:30 (동시호가 직전) 에는 **신규 매수만 차단**.
청산은 그대로 실행 — 비정상 호가에 당하지 않도록 방어.

### 7. 일일 LLM 비용 한도
`llm.daily_cost_limit_usd` 도달 시 해당일 LLM 호출 전면 중단. 사이클은 계속 돌지만
후보 종목에 대한 Claude 판단 생성만 멈춥니다.

### 8. 체결 확인 + 미체결 매수 자동 취소
시장가 주문 후 30초 대기 → KIS `inquire-daily-ccld` 로 일괄 조회 → 미체결 매수는
`cancel_order` 로 자동 취소 (다음 사이클 재판단). 미체결 판매는 손절/청산일 수 있어
그대로 대기.

### 9. 사후 정확도 트래킹
평일 16:30 KST 에 5 거래일 경과한 buy/sell signal 의 forward return 계산 →
`signals.realized_return_pct` 에 기록. `/accuracy` 로 confidence bucket 별 적중률 +
교차검증 태그 집계 조회. 적중률이 낮으면 `confidence_threshold` 상향 검토.

---

## 🔧 트러블슈팅

<details>
<summary><b>KIS API 관련</b></summary>

### "초당 거래건수를 초과하였습니다" (HTTP 500)

KIS 는 rate limit 에러를 공식 문서와 달리 **HTTP 500 + JSON 바디** 로 반환합니다.
`kis/client.py` 가 바디를 먼저 파싱하고 `msg1` 에 "초당" 포함 여부로 판정 후 0.5초 백오프
재시도합니다. 기본 간격:

- 실전: `live_min_interval_sec=0.055` (≈18 req/s, 한도 20 의 90%)
- 모의: `paper_min_interval_sec=0.55` (≈1.8 req/s, 한도 2 의 90%)

신규 API 발급 후 3일간은 실전이 3 req/s 로 제한 → `settings.yaml` 에서 `live_min_interval_sec`
를 `0.34` 로 일시 조정 후 3일 뒤 복원.

### 모의 서버에서 시세 조회가 종종 500

KIS 모의 서버(`openapivts`)의 국내주식 시세 API 는 불안정. 봇은 모드가 paper 여도
**시세 조회는 항상 실전 서버 + 실전 키로** 보냅니다. 주문/잔고만 현재 모드 서버로.
이 이원화가 동작하려면 `.env` 에 `KIS_LIVE_*` 키도 함께 세팅되어 있어야 합니다.

### "모의투자 장종료 입니다" (msg_cd=40580000)

KIS 모의 서버는 장외 시간 주문을 큐잉하지 않습니다. 스케줄러가 평일 09:00~15:30 으로
제한되어 있지만, `--once --force` 수동 실행 시에는 주문 거절 가능.

### 모의 계좌 만료 (90일)

KIS 모의 계좌는 90일 유효. 재신청 시 새 앱키/시크릿/계좌번호가 발급됩니다. 봇이 7일
전부터 자동 경고하고, `/setcreds paper KEY SECRET ACCOUNT` 한 줄로 런타임 교체 가능
(Docker 재시작 불필요).

</details>

<details>
<summary><b>Claude API / Telegram</b></summary>

### "Your credit balance is too low to access the Anthropic API"

Billing 페이지에 크레딧이 있는데도 이 에러가 나오면 **API 키를 재발급**하세요. 일부
케이스에서 서버 쪽에 "잔고 부족" 상태가 캐싱되어 크레딧 충전 후에도 해제되지 않는 현상이
관측됩니다.

### Claude API BadRequestError at tool_choice

Anthropic SDK 버전이 낮으면 `tool_choice={"type":"tool","name":...}` 포맷이 인식되지 않을
수 있습니다. `pip install --upgrade "anthropic>=0.40"` 로 최신화.

### 텔레그램 `/start` 만 보냈을 때 `getUpdates` 가 비어있음

일부 Telegram 클라이언트는 `/start` 를 특수 처리해서 서버에 전달 안 될 수 있음. 평문
메시지(예: `hi`) 하나 보내면 즉시 update 가 잡힙니다.

### 텔레그램 기동 메시지가 안 옴

`.env` 의 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 오타 가능성. 컨테이너 내부 확인:
```bash
sudo docker compose exec trading-bot env | grep TELEGRAM
```

</details>

<details>
<summary><b>Docker / NAS / SSH</b></summary>

### `scp: subsystem request failed on channel 0`

Synology 에 SFTP 서브시스템이 꺼져있음. `scp -O` 로 레거시 프로토콜 사용. 또는 DSM →
File Station → 설정 → SFTP 탭 에서 SFTP 서비스 활성화.

### `ssh: Could not resolve hostname`

mDNS 가 풀리지 않음. `DREAM.local` 대신 직접 IP 사용 (예: `192.168.0.148`).

### `Connection refused` on port 22

SSH 서비스 포트가 22 가 아님. DSM → 제어판 → 터미널 및 SNMP 에서 포트 확인 후
`-p <포트>` (ssh) 또는 `-P <포트>` (scp) 로 지정.

### 긴 URL 명령이 자동으로 줄바꿈됨

`sudo curl -o 파일 URL` 에서 URL 부분이 터미널에서 잘려 두 줄로 해석되는 경우. 변수로
쪼개서 실행:
```bash
URL=https://raw.githubusercontent.com/.../파일.yaml
sudo curl -o 파일.yaml "$URL"
```

### `permission denied while trying to connect to Docker daemon socket`

현재 사용자가 docker 그룹 멤버가 아님. `sudo` 붙여서 실행하거나:
```bash
sudo synogroup --add docker $USER
```
(DSM 에서 그룹 추가 후 재로그인)

### `pull access denied`

GHCR 패키지가 private 상태거나 경로 오타. `docker-compose.yml` 의 `image:` 라인 확인.
Public 확인: 브라우저로 `https://ghcr.io/v2/hdream0322/trading/tags/list` 접속 시 태그
목록이 보이면 public.

### Synology sudo 에 docker 경로 없음

`sudo docker` 가 "command not found". 해결: `sudo /usr/local/bin/docker ...` 절대 경로 사용.

</details>

---

## 📁 프로젝트 구조

<details>
<summary><b>디렉토리 트리</b></summary>

```
trading/
├─ Dockerfile                    python:3.11-slim, TZ=Asia/Seoul
├─ docker-compose.yml            trading-bot + watchtower + volumes
├─ pyproject.toml
├─ .env.example                  시크릿 템플릿
├─ .gitignore
├─ README.md
├─ CLAUDE.md                     프로젝트 가이드 (Claude Code 용)
├─ config/
│   ├─ settings.yaml             universe / 사이클 / 리스크 / LLM / rate_limit / exit 파라미터
│   └─ market_holidays.yaml      KRX 휴장일 (연 1회 갱신)
├─ scripts/
│   ├─ stage3_verify.py          주문 실행 경로 E2E 검증
│   ├─ stage4_verify.py          텔레그램 커맨드 핸들러 단위 검증
│   └─ stage6_verify.py          exit_strategy.check_exit 시나리오
├─ .github/workflows/
│   ├─ docker-publish.yml        main/tag push → GHCR 빌드
│   └─ release.yml               v* tag → GitHub Release 자동 생성
└─ trading_bot/
    ├─ __init__.py               __version__ (BOT_VERSION env 우선)
    ├─ main.py                   엔트리 + APScheduler 9 크론
    ├─ smoke_test.py             Stage 1 검증
    ├─ config.py                 .env + settings.yaml 로딩
    ├─ logging_setup.py
    ├─ kis/
    │   ├─ auth.py               토큰 발급/캐시/자동 갱신
    │   └─ client.py             REST (시세/잔고/OHLCV/주문/취소/hashkey)
    ├─ signals/
    │   ├─ indicators.py         RSI, volume_ratio, SMA, ATR
    │   ├─ prefilter.py          룰베이스 + 추세 필터
    │   ├─ llm.py                Claude API (side_hint 없음, 프롬프트 캐싱)
    │   ├─ exit_strategy.py      손절/익절/트레일링 (ATR 동적 손절 지원)
    │   ├─ fill_tracker.py       체결 확인 + 미체결 매수 자동 취소
    │   ├─ accuracy.py           사후 정확도 트래킹 (5 거래일 forward return)
    │   ├─ briefing.py           장 시작/마감 브리핑
    │   └─ cycle.py              전체 오케스트레이션 (4-Gate + 셔플 + 섹터)
    ├─ risk/
    │   ├─ manager.py            RiskManager (8단계 게이트 + 섹터 분산)
    │   └─ kill_switch.py        파일 기반 (수동/자동 구분 + 복구)
    ├─ bot/
    │   ├─ commands.py           텔레그램 커맨드 핸들러 (18개)
    │   ├─ poller.py             long polling + 멀티라인 커맨드
    │   ├─ context.py            BotContext (공유 상태 + trading_lock)
    │   ├─ update_manager.py     자동 업데이트 + GHCR digest 비교
    │   ├─ mode_switch.py        런타임 모드 오버라이드
    │   ├─ quiet_mode.py         /quiet 토글
    │   ├─ expiry.py             90일 모의 계좌 카운트다운
    │   ├─ universe_helper.py    섹터 자동 백필 + 카운트 (v0.5.0)
    │   └─ runtime_state.py      모듈 간 공유 상태
    ├─ store/
    │   ├─ db.py                 SQLite 스키마 + 마이그레이션
    │   ├─ repo.py               insert/query 헬퍼 (사후 정확도 집계 포함)
    │   └─ backup.py             Online Backup API (7일 롤링)
    ├─ notify/
    │   └─ telegram.py           sendMessage, getUpdates, inline keyboard
    └─ utils/
        └─ calendar_kr.py        KRX 휴장일 + 장시간 판정
```

</details>

---

## 🗺 개발 로드맵

- [x] **Stage 1** — 골격 + KIS 인증 + 시세/잔고 + 텔레그램
- [x] **Stage 2** — 룰베이스 + Claude LLM 시그널 + DB
- [x] **Stage 3** — 주문 실행 + 리스크 매니저 + 킬 스위치
- [x] **Stage 4** — 텔레그램 양방향 제어
- [x] **Stage 5** — NAS Docker 배포
- [x] **Stage 6** — 손절/익절/트레일링 자동 청산
- [x] **v0.2.8** — 런타임 제어 강화 (`/mode`, `/reload`, `/restart`, `/setcreds`)
- [x] **v0.3.x** — 알림 정책 개편 (`/quiet`, 장 시작/마감 브리핑)
- [x] **v0.4.0** — 신뢰성/관찰성 (체결 추적, 회로차단기, pnl_daily, 백업, 프롬프트 캐싱)
- [x] **v0.5.0** — 거래 판단 로직 개편 + 신뢰성/관찰성 확장
  - 분산 강화 (동시 5종목, 섹터 분산), 추세 필터, ATR 동적 손절
  - LLM 독립 판단 + 교차검증, 유니버스 셔플, 변동성 구간 차단
  - 사후 정확도 트래킹 + `/accuracy`, 30초 체결 확인 + 미체결 자동 취소
  - 킬스위치 자동 복구, KIS throttle 설정화, 멀티라인 커맨드
- [ ] **Stage 7** — 웹 대시보드 (FastAPI + 차트)
- [ ] **Stage 8** — Lean 엔진 백테스트 통합, 전략 튜닝
- [ ] 추가 아이디어 — 뉴스 헤드라인 감성 분석, 펀더멘털 데이터, 실시간 웹소켓 시세

---

## 📜 라이선스

**GNU General Public License v3.0 (GPL v3)**

<details>
<summary><b>전체 내용 펼치기</b></summary>

이 프로젝트는 [GNU GPL v3](https://www.gnu.org/licenses/gpl-3.0.html) 라이선스로 배포됩니다.
역사상 가장 유명한 copyleft 라이선스 중 하나로, Linux 커널, GCC, Git 등이 GPL 계열을
사용합니다.

| | 허용 / 의무 | 설명 |
|---|---|---|
| ✅ | 자유 사용 | 개인·교육·연구·상업 목적 모두 허용 |
| ✅ | 복사·배포 | 원본 또는 수정본 모두 배포 가능 |
| ✅ | 수정·파생 저작물 | 코드 수정 및 2차 저작물 생성 가능 |
| ✅ | **상업적 이용 허용** | 유료 서비스, 유료 배포, 회사 내부 사용 등 전부 가능 |
| 🔁 | **소스 공개 필수 (copyleft)** | 수정본을 배포하거나 서비스로 제공할 때 **소스 코드를 동일하게 GPL v3 로 공개 필수** |
| 📌 | **저작자·라이선스 고지 유지** | 원저작권·라이선스 표시·수정 이력 명시 |
| 🛡️ | **특허 라이선스 자동 부여** | 기여자가 가진 특허는 사용자에게 자동 허용 |
| ❌ | **tivoization 금지** | 수정 가능한 라이선스인데 기기 차원에서 재설치를 막을 수 없음 |

전체 법률 조항은 [LICENSE](LICENSE) 파일 (공식 전문) 또는
[GNU GPL v3 공식 페이지](https://www.gnu.org/licenses/gpl-3.0.html) 참고.

**의미하는 것**:

- ✅ 누구나 이 코드를 가져다가 **자유롭게 사용·수정·상업화 가능**합니다. 유료 서비스로
  만들어 돈을 벌어도 OK.
- 🔁 하지만 **수정한 버전을 배포·서비스하려면 소스 코드도 반드시 함께 공개**해야 합니다.
  GPL 은 "받은 자유는 전파돼야 한다" 는 철학입니다.
- 🔁 수정본은 **반드시 같은 GPL v3** 로 배포해야 합니다 (다른 라이선스로 재라이선스 불가).
- 📌 수정 이력과 원저작권은 유지해야 합니다 — 원저작자를 지우고 본인 것으로 위장 불가.

> ℹ️ **AGPL v3 대안**: GPL v3 는 "바이너리 배포" 시점에만 소스 공개 의무가 발생합니다.
> 즉 누군가 이 봇을 수정해서 SaaS (클라우드 서비스) 로 제공하면서 바이너리는 안 배포하면
> 소스 공개 의무를 회피할 수 있습니다. 이 "SaaS 루프홀" 을 막고 싶다면 AGPL v3 로 업그레이드
> 가능합니다. MongoDB(이전), Grafana, Elasticsearch 등이 AGPL 을 채택한 이유가 이 때문입니다.

</details>

**면책 조항**: 본 소프트웨어는 "있는 그대로(AS IS)" 제공되며, 명시적이든 묵시적이든 어떠한
보증도 없습니다. 저작자는 소프트웨어 사용으로 인해 발생하는 **어떠한 손실·손해·청구·기타
책임에 대해서도 일체 책임지지 않습니다**. 특히 주식 거래 손실, API 키 유출, 시스템 장애,
잘못된 매매 판단 등 모든 금전적·기술적 피해는 사용자 본인의 책임입니다. 실거래 전환 전에는
반드시 모의투자(`KIS_MODE=paper`)로 최소 4주 이상 검증하세요.
