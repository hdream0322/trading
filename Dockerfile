FROM python:3.11-slim

# GitHub Actions가 빌드 시 git tag 또는 branch 이름을 전달.
# 로컬 `docker build` 에선 기본값 dev 사용.
ARG BOT_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    PIP_NO_CACHE_DIR=1 \
    BOT_VERSION=${BOT_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

WORKDIR /app

# 의존성은 requirements.txt 의 명시적 핀으로 설치 → 매 빌드마다 PyPI 최신
# patch 가 자동으로 끌려와 회귀/공급망 위험에 노출되던 문제 차단.
# 갱신 시 로컬 venv 의 `pip freeze` 결과를 requirements.txt 에 반영.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY trading_bot/ ./trading_bot/
COPY config/ ./config/
# 기본 설정 스냅샷 — /config reset 시 이 경로에서 복원.
# bind mount 로 덮이지 않는 별도 경로라 런타임에 항상 접근 가능.
COPY config/ ./config_defaults/
COPY scripts/ ./scripts/

CMD ["python", "-m", "trading_bot.main"]
